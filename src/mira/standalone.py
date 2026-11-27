"""One QQ Official Bot connection and background work owned by Mira."""

import asyncio
import json
import os
import traceback
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4

import httpx
from qqbot_agent_sdk import (EventParser, QQApiClient, QQWebSocket,
                             WSCallbacks, WSSessionStore, start_onboard)

from .admin_config import AdminConfig
from .config import load_character, load_settings
from .conversation import ConversationService
from .memory import MemoryService
from .models import MessageInput, MiraError
from .provider import OpenAICompatibleProvider
from .proactive import ProactiveService, proactive_result_message
from .storage import ConversationRepository


USER_ID = "local-user"


class RuntimeLog:
    """Write a bounded, chronological service log without chat text or keys."""

    def __init__(self, root):
        self.path = Path(root) / "data" / "mira.log"
        self._lock = Lock()

    def write(self, level, source, message, code=None, detail=None):
        at = datetime.now().astimezone().isoformat(timespec="seconds")
        lines = [f"{at} {level:<5} [{source}] {message}"
                 + (f" ({code})" if code else "")]
        if detail:
            for key in ("stage", "exception", "reason"):
                if detail.get(key):
                    value = str(detail[key]).replace("\r", "\\r").replace("\n", "\\n")
                    lines.append(f"{at} {level:<5} [{source}]   {key}: {value}")
            for frame in detail.get("stack", []):
                lines.append(f"{at} {level:<5} [{source}]   at "
                             f"{frame['file']}:{frame['line']} {frame['function']}")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > 2_000_000:
                os.replace(self.path, self.path.with_name("mira.previous.log"))
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write("\n".join(lines) + "\n")

    def tail(self, limit=500):
        with self._lock:
            if not self.path.exists():
                return []
            with self.path.open("r", encoding="utf-8") as stream:
                return [line.rstrip("\n") for line in deque(stream, maxlen=limit)]


class EventLog:
    """Keep operational events without storing chat text or credentials."""

    def __init__(self, root, runtime_log=None):
        self.path = Path(root) / "data" / "server_events.jsonl"
        self._lock = Lock()
        self.runtime_log = runtime_log

    def add(self, category, message, code=None, detail=None, level="INFO"):
        entry = {"at": datetime.now(timezone.utc).isoformat(),
                 "category": category, "message": message, "code": code}
        if detail is not None:
            entry["detail"] = detail
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > 2_000_000:
                previous = self.path.with_suffix(".previous.jsonl")
                os.replace(self.path, previous)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if self.runtime_log is not None:
            self.runtime_log.write(level, category, message, code, detail)

    @staticmethod
    def failure_detail(exc, stage, *, include_reason=False, secrets=()):
        """Keep useful failure locations without capturing private chat text."""
        reason = str(exc) if include_reason else str(
            getattr(exc, "code", type(exc).__name__)
        )
        for secret in secrets:
            if isinstance(secret, str) and len(secret) >= 4:
                reason = reason.replace(secret, "[已隐藏]")
        frames = traceback.extract_tb(exc.__traceback__)[-12:]
        return {"stage": stage, "exception": type(exc).__name__,
                "reason": reason[:1000],
                "stack": [{"file": frame.filename, "line": frame.lineno,
                           "function": frame.name} for frame in frames]}

    def recent(self, limit=100):
        with self._lock:
            if not self.path.exists():
                return []
            with self.path.open("r", encoding="utf-8") as stream:
                lines = deque(stream, maxlen=limit)
        events = []
        for line in reversed(lines):
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        return events


class QQConnection:
    """Translate one authorized C2C conversation into Mira turns."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.api = None
        self.http_client = None
        self.websocket = None
        self.status = "disconnected"
        self.last_error = None
        self.pending_user = None
        self._message_lock = asyncio.Lock()

    async def connect(self):
        if self.websocket is not None:
            return
        bot = self.runtime.config.bot()
        if not bot.get("app_id") or not bot.get("secret"):
            raise MiraError("bot_not_configured")
        self.status = "connecting"
        self.last_error = None
        http_client = None
        api = None
        stage = "token"
        try:
            self.runtime.logs.write("INFO", "qq", "开始获取 QQ 令牌")
            api = QQApiClient(app_id=bot["app_id"], client_secret=bot["secret"])
            http_client = httpx.AsyncClient()
            api.setup(http_client)
            await api.ensure_token()
            self.runtime.logs.write("INFO", "qq", "QQ 令牌获取成功")
            stage = "gateway"
            self.runtime.logs.write("INFO", "qq", "开始获取 QQ 网关地址")
            gateway_url = await api.get_gateway_url()
            self.runtime.logs.write("INFO", "qq", "QQ 网关地址获取成功")
            session_store = WSSessionStore(str(self.runtime.root / "data"))

            def get_session():
                saved = session_store.get(bot["app_id"])
                return saved.session_id or None, saved.seq

            def set_session(session_id, sequence):
                if session_id:
                    session_store.save(bot["app_id"], session_id, sequence)
                else:
                    session_store.clear(bot["app_id"])

            callbacks = WSCallbacks(
                on_message_event=self.on_message,
                on_connected=self._connected,
                on_disconnected=self._disconnected,
                on_fatal_error=self._fatal_error,
                get_token=api.ensure_token_sync,
                get_session=get_session,
                set_session=set_session,
                set_heartbeat_interval=lambda interval: None,
                clear_token=api.clear_token,
                fail_pending=lambda reason: None,
                get_gateway_url=api.get_gateway_url_sync,
            )
            websocket = QQWebSocket(callbacks=callbacks)
            stage = "websocket"
            self.runtime.logs.write("INFO", "qq", "启动 QQ WebSocket")
            websocket.start(gateway_url, asyncio.get_running_loop())
            self.api = api
            self.http_client = http_client
            self.websocket = websocket
            self.runtime.events.add("qq", "正在连接 QQ 官方机器人")
        except Exception as exc:
            if http_client is not None:
                await http_client.aclose()
            self.status = "error"
            self.last_error = "qq_" + stage + "_failed"
            detail = EventLog.failure_detail(
                exc, stage, include_reason=True,
                secrets=(bot.get("app_id"), bot.get("secret"),
                         bot.get("allowed_user_id"),
                         getattr(api, "access_token", None)),
            )
            self.runtime.events.add("qq", "QQ 连接失败", self.last_error,
                                    detail, level="ERROR")
            raise MiraError(self.last_error) from exc

    async def disconnect(self):
        websocket = self.websocket
        http_client = self.http_client
        self.websocket = None
        self.api = None
        self.http_client = None
        try:
            if websocket is not None:
                await websocket.async_stop()
        finally:
            if http_client is not None:
                await http_client.aclose()
        self.status = "disconnected"
        self.runtime.events.add("qq", "已停止 QQ 连接")

    def _connected(self):
        self.status = "connected"
        self.last_error = None
        self.runtime.events.add("qq", "QQ 连接已建立")

    def _disconnected(self):
        if self.websocket is not None:
            self.status = "reconnecting"
            self.runtime.events.add("qq", "QQ 连接中断，正在重连")

    def _fatal_error(self, code, message):
        self.status = "error"
        self.last_error = str(code)
        bot = self.runtime.config.bot()
        reason = str(message)
        for secret in (bot.get("app_id"), bot.get("secret"),
                       bot.get("allowed_user_id"),
                       getattr(self.api, "access_token", None)):
            if isinstance(secret, str) and len(secret) >= 4:
                reason = reason.replace(secret, "[已隐藏]")
        self.runtime.events.add("qq", "QQ 连接出现不可恢复的错误", str(code),
                                {"stage": "websocket", "reason": reason[:1000]},
                                level="ERROR")

    async def on_message(self, event_type, raw):
        event = EventParser().parse(event_type, raw)
        if event is None or event.chat_scope != "c2c" or not event.content.strip():
            return
        bot = self.runtime.config.bot()
        allowed = bot.get("allowed_user_id")
        if not allowed:
            self.pending_user = {"user_id": event.user_id,
                                 "name": event.user_name or "QQ 用户"}
            self.runtime.events.add("qq", "收到待绑定的私聊账号")
            return
        if event.user_id != allowed:
            self.runtime.events.add("qq", "忽略未绑定账号的消息")
            return
        self.runtime.logs.write("INFO", "chat", "收到已绑定账号的私聊消息")
        async with self._message_lock:
            try:
                response = await asyncio.to_thread(self._handle_message, event, bot["app_id"])
                if response is None:
                    return
                reply, quote_current_message = response
                if self.api is None:
                    raise MiraError("qq_disconnected")
                message = self.api.build_text_body(
                    reply, reply_to=event.message_id, markdown=False
                )
                if not quote_current_message:
                    message.message_reference = None
                await self.api.post_c2c_message(event.chat_id, message)
                self.runtime.events.add("chat", "私聊已回复")
            except Exception as exc:
                self.runtime.events.add("chat", "私聊处理或投递失败",
                                        getattr(exc, "code", type(exc).__name__),
                                        EventLog.failure_detail(exc, "chat"),
                                        level="ERROR")

    def _handle_message(self, event, app_id):
        settings = load_settings(self.runtime.root)
        character = load_character(settings.character_path)
        repository = ConversationRepository(settings.database_path)
        try:
            service = ConversationService(settings, repository,
                                          OpenAICompatibleProvider(settings))
            service.prepare()
            source = "qq:" + app_id
            if repository.find_turn_by_source(
                USER_ID, character["character_id"], source, event.message_id
            ):
                return None
            try:
                occurred_at = datetime.fromisoformat(
                    event.timestamp.replace("Z", "+00:00")
                ).astimezone(timezone.utc).isoformat()
            except (ValueError, AttributeError):
                occurred_at = datetime.now(timezone.utc).isoformat()
            result = service.handle_message(MessageInput(
                USER_ID, character["character_id"], source, event.message_id,
                event.content, occurred_at,
            ))
            if result.status != "completed":
                self.runtime.events.add("chat", "本轮生成失败", result.error_code,
                                        level="ERROR")
                return "这条消息处理失败了，请稍后重新发送。", False
            return result.text, result.quote_current_message
        finally:
            repository.close()

    async def send_proactive(self, text):
        bot = self.runtime.config.bot()
        if self.api is None or self.status != "connected" or not bot.get("allowed_user_id"):
            raise MiraError("qq_disconnected")
        await self.api.send_text("c2c", bot["allowed_user_id"], text,
                                 markdown=False, retries=1)
        self.runtime.events.add("proactive", "主动消息已提交 QQ")


class MiraRuntime:
    """Own the server's single QQ connection, onboarding, and maintenance loop."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.config = AdminConfig(self.root)
        self.logs = RuntimeLog(self.root)
        self.events = EventLog(self.root, self.logs)
        self.qq = QQConnection(self)
        self.onboard = {"status": "idle", "qr_ready": False}
        self._qr_url = None
        self._onboard_task = None
        self._worker_task = None
        self._connect_task = None
        self._stop = asyncio.Event()
        self.loop = None

    async def start(self):
        self.loop = asyncio.get_running_loop()
        self._stop.clear()
        self.logs.write("INFO", "service", "Mira 服务启动")
        await asyncio.to_thread(self._recover_once)
        self._worker_task = asyncio.create_task(self._worker())
        if self.config.bot().get("enabled"):
            self._connect_task = asyncio.create_task(self._connect_on_start())

    async def _connect_on_start(self):
        try:
            await self.qq.connect()
        except Exception:
            pass

    async def stop(self):
        self._stop.set()
        for task in (self._onboard_task, self._worker_task, self._connect_task):
            if task and not task.done():
                task.cancel()
        for task in (self._onboard_task, self._worker_task, self._connect_task):
            if not task:
                continue
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self.qq.disconnect()
        self.logs.write("INFO", "service", "Mira 服务已停止")

    async def _worker(self):
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self.run_due)
            except Exception as exc:
                self.events.add("worker", "后台检查失败",
                                getattr(exc, "code", type(exc).__name__),
                                EventLog.failure_detail(exc, "worker"),
                                level="ERROR")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass

    def run_due(self):
        settings = load_settings(self.root, require_model=False)
        if not all((settings.base_url, settings.api_key, settings.model)):
            return
        character = load_character(settings.character_path)
        repository = ConversationRepository(settings.database_path)
        try:
            provider = OpenAICompatibleProvider(settings)
            service = ConversationService(settings, repository, provider)
            service.prepare()
            count = MemoryService(settings, repository, provider).run_due()
            if count:
                self.events.add("memory", "已整理对话记忆")
            bot = self.config.bot()
            if (self.qq.status == "connected" and bot.get("allowed_user_id") and
                repository.proactive_enabled(USER_ID, character["character_id"])):
                def deliver(text):
                    future = asyncio.run_coroutine_threadsafe(
                        self.qq.send_proactive(text), self.loop
                    )
                    future.result(timeout=30)

                result = ProactiveService(settings, repository, provider, deliver,
                                          should_stop=self._stop.is_set).check(
                    USER_ID, character["character_id"]
                )
                if result not in {"quiet_hours", "already_checked", "disabled"}:
                    self.events.add("proactive", "主动联系检查完成", result,
                                    {"reason": proactive_result_message(result)})
        finally:
            repository.close()

    def _recover_once(self):
        try:
            settings = load_settings(self.root, require_model=False)
            character = load_character(settings.character_path)
            repository = ConversationRepository(settings.database_path)
            try:
                repository.bind_character(character["character_id"],
                                          settings.character_path)
                repository.recover_pending()
            finally:
                repository.close()
        except MiraError:
            pass

    def start_onboarding(self, replace=False):
        if self._onboard_task and not self._onboard_task.done():
            raise MiraError("onboarding_in_progress")
        if self.config.bot().get("app_id") and not replace:
            raise MiraError("bot_replacement_requires_confirmation")
        self.onboard = {"status": "starting", "qr_ready": False}
        self._qr_url = None
        self._replace_onboard = replace
        self._onboard_task = asyncio.create_task(self._run_onboarding())

    async def _run_onboarding(self):
        try:
            def on_qr_ready(url):
                self._qr_url = url
                self.onboard = {"status": "waiting_for_scan", "qr_ready": True}

            result = await start_onboard(on_qr_ready=on_qr_ready)
            self.config.save_bot(result.app_id, result.client_secret,
                                 result.user_openid, enabled=False,
                                 replace=self._replace_onboard)
            self.onboard = {"status": "completed", "qr_ready": False,
                            "app_id": result.app_id}
            self._qr_url = None
            self.events.add("qq", "扫码接入完成，等待启动连接")
        except asyncio.CancelledError:
            self.onboard = {"status": "cancelled", "qr_ready": False}
            self._qr_url = None
            raise
        except Exception as exc:
            self.onboard = {"status": "failed", "qr_ready": False,
                            "error": type(exc).__name__}
            self._qr_url = None
            self.events.add("qq", "扫码接入失败", type(exc).__name__,
                            EventLog.failure_detail(exc, "onboarding"),
                            level="ERROR")

    def qr_url(self):
        return self._qr_url
