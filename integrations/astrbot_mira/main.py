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
QQ_SEND_TEST_MESSAGE = "【Mira 主动发送测试】小墨，我来找你聊聊天。今天过得怎么样？"
PROACTIVE_RESULT_MESSAGES = {
    "disabled": "主动联系尚未开启",
    "paused": "主动联系已暂停",
    "quiet_hours": "当前不在主动联系时段（09:00—22:00）",
    "daily_limit": "今天已达到主动联系上限",
    "recent_conversation": "最近六小时聊过天，暂不主动联系",
    "awaiting_reply": "上一条主动消息尚未得到回复，暂不重复联系",
    "no_conversation": "暂无可参考的对话",
    "already_checked": "当前时段已经检查过，不会重复检查",
    "skipped": "模型决定这次不联系",
    "sent": "已发送主动消息",
    "boundary_changed": "发送前条件发生变化，本次未发送",
    "stopped": "检查已停止，本次未发送",
    "avoided_topic": "拟发送内容涉及你不希望讨论的话题，本次未发送",
    "delivery_unknown": "发送结果不确定，请查看聊天记录；本次不会自动重发",
    "invalid_proactive_decision": "模型的判断结果无效，本次未发送",
}


def _proactive_result_message(result):
    """Translate locally because AstrBot can reload this plugin without reloading Mira core."""
    return PROACTIVE_RESULT_MESSAGES.get(result, "检查未完成，请查看运行日志")


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
        # AstrBot removes the wake prefix from event.message_str, but not the original QQ message.
        raw_text = event.message_obj.message_str.strip()
        if not text:
            event.stop_event()
            return
        try:
            async with self._message_lock:
                if raw_text == "/mira" or raw_text.startswith("/mira "):
                    reply = await self._command(raw_text)
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
        if action == "test":
            try:
                await self._send_qq_message(QQ_SEND_TEST_MESSAGE)
            except Exception:
                logger.exception("Mira QQ outbound test failed")
                return "主动发送测试失败，请查看 AstrBot 日志。"
            return "主动发送测试已提交，请确认是否收到单独的一条测试消息。"
        return ("可用命令：/mira on（开启）、/mira off（关闭）、"
                "/mira status（查看状态）、/mira preview（仅预览）、"
                "/mira check（立即检查）、/mira test（测试 QQ 主动发送）")

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
                if choice == "skip":
                    return "预览（未发送）：模型决定这次不联系。原因：" + reason
                return "预览（未发送）：" + _proactive_result_message(choice)
            result = proactive.check(USER_ID, character["character_id"])
            return "主动联系检查：" + _proactive_result_message(result)
        finally:
            repository.close()

    def _proactive_service(self, settings, repository, loop):
        def deliver(text):
            future = asyncio.run_coroutine_threadsafe(
                self._send_qq_message(text), loop
            )
            try:
                future.result(timeout=30)
            except FutureTimeout as exc:
                raise RuntimeError("QQ delivery timed out") from exc

        return ProactiveService(
            settings, repository, OpenAICompatibleProvider(settings), deliver,
            should_stop=self._stop.is_set,
        )

    async def _send_qq_message(self, text):
        if not await self.context.send_message(self.target_umo, MessageChain().message(text)):
            raise RuntimeError("QQ session unavailable")

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
