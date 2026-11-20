"""Route one QQ Official private chat through Mira inside AstrBot."""

import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

from mira.config import load_character, load_settings
from mira.conversation import ConversationService
from mira.models import MessageInput
from mira.provider import OpenAICompatibleProvider
from mira.proactive import ProactiveService
from mira.storage import ConversationRepository


USER_ID = "local-user"


class MiraQQPlugin(Star):
    """Use AstrBot for QQ transport and Mira for conversation decisions."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.root = Path(config.get("mira_root", "")).expanduser()
        self.target_umo = config.get("target_umo", "").strip()
        self._stop = Event()
        self._ready = False
        self._ready_lock = Lock()
        self._message_lock = asyncio.Lock()
        self._task = None

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        self._start_timer()

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=10)
    @filter.platform_adapter_type(filter.PlatformAdapterType.QQOFFICIAL)
    async def on_private_message(self, event: AstrMessageEvent):
        if (not self.target_umo or not self.root.is_absolute() or
            event.unified_msg_origin != self.target_umo):
            return
        self._start_timer()
        text = event.message_str.strip()
        if not text:
            event.stop_event()
            return
        try:
            async with self._message_lock:
                if text == "/mira" or text.startswith("/mira "):
                    reply = await self._command(text)
                else:
                    message_id = str(event.message_obj.message_id)
                    when = datetime.fromtimestamp(
                        event.message_obj.timestamp, timezone.utc
                    ).isoformat()
                    reply = await asyncio.to_thread(self._handle_message, message_id, text, when)
        except Exception:
            logger.exception("Mira QQ message handling failed")
            reply = "这条消息处理失败了，请稍后重新发送。"
        if reply:
            yield event.plain_result(reply)
        event.stop_event()

    async def _command(self, text):
        action = text.removeprefix("/mira").strip()
        if action in {"on", "off", "status"}:
            return await asyncio.to_thread(self._setting_command, action)
        if action in {"preview", "check"}:
            return await asyncio.to_thread(self._proactive_command, action,
                                           asyncio.get_running_loop())
        return "用法：/mira on|off|status|preview|check"

    def _runtime(self):
        if not self.root.is_absolute() or not self.target_umo:
            raise ValueError("Mira plugin requires an absolute mira_root and target_umo")
        settings = load_settings(self.root)
        character = load_character(settings.character_path)
        return settings, character

    def _repository(self, settings, character):
        repository = ConversationRepository(settings.database_path)
        try:
            with self._ready_lock:
                if not self._ready:
                    service = ConversationService(
                        settings, repository, OpenAICompatibleProvider(settings)
                    )
                    service.prepare()
                    service.recover()
                    self._ready = True
            return repository
        except Exception:
            repository.close()
            raise

    def _handle_message(self, message_id, text, when):
        settings, character = self._runtime()
        repository = self._repository(settings, character)
        try:
            source = "qq:" + self.target_umo
            if repository.find_turn_by_source(
                USER_ID, character["character_id"], source, message_id
            ):
                return None
            service = ConversationService(
                settings, repository, OpenAICompatibleProvider(settings)
            )
            result = service.handle_message(MessageInput(
                USER_ID, character["character_id"], source,
                message_id, text, when,
            ))
            if result.status != "completed":
                return "这条消息处理失败了，请稍后重新发送。"
            return result.text
        finally:
            repository.close()

    def _setting_command(self, action):
        settings, character = self._runtime()
        repository = self._repository(settings, character)
        try:
            user_id = USER_ID
            character_id = character["character_id"]
            if action != "status":
                repository.set_proactive_enabled(user_id, character_id, action == "on")
            enabled = repository.proactive_enabled(user_id, character_id)
            paused = repository.get_contact_pause(
                user_id, character_id, datetime.now(timezone.utc)
            ) is not None
            return ("主动联系：" + ("开启" if enabled else "关闭") +
                    "；暂停：" + ("是" if paused else "否"))
        finally:
            repository.close()

    def _proactive_command(self, action, loop):
        settings, character = self._runtime()
        repository = self._repository(settings, character)
        try:
            proactive = self._proactive_service(settings, repository, loop)
            if action == "preview":
                choice, content, reason = proactive.preview(USER_ID, character["character_id"])
                if choice == "send":
                    return "预览（未发送）：" + content + "\n原因：" + reason
                return "预览（未发送）：" + (reason or choice)
            result = proactive.check(USER_ID, character["character_id"])
            return "主动联系检查：" + result
        finally:
            repository.close()

    def _proactive_service(self, settings, repository, loop):
        def deliver(text):
            future = asyncio.run_coroutine_threadsafe(
                self.context.send_message(self.target_umo, MessageChain().message(text)), loop
            )
            try:
                if not future.result(timeout=30):
                    raise RuntimeError("QQ session unavailable")
            except FutureTimeout as exc:
                raise RuntimeError("QQ delivery timed out") from exc

        return ProactiveService(
            settings, repository, OpenAICompatibleProvider(settings), deliver,
            should_stop=self._stop.is_set,
        )

    def _start_timer(self):
        if self.target_umo and self.root.is_absolute() and not self._stop.is_set():
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._timer_loop())

    async def _timer_loop(self):
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self._proactive_command, "check", loop)
            except Exception:
                logger.exception("Mira QQ proactive check failed")
            await asyncio.sleep(60)

    async def terminate(self):
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
