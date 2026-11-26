import asyncio
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mira.config import load_settings
from mira.conversation import ConversationService
from mira.models import MessageInput
from mira.storage import ConversationRepository


try:
    import astrbot.api  # noqa: F401
except ImportError:
    plugin_module = None
else:
    plugin_path = Path(__file__).resolve().parents[1] / "integrations/astrbot_mira/main.py"
    spec = importlib.util.spec_from_file_location("astrbot_mira_test", plugin_path)
    plugin_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin_module)


class FakeProvider:
    chat_calls = 0

    def __init__(self, _settings):
        pass

    def generate(self, _messages, tools=None):
        if tools and tools[0]["function"]["name"] == "propose_proactive":
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "skip", "type": "function", "function": {
                    "name": "skip_proactive", "arguments": json.dumps({"reason": "测试跳过"}),
                },
            }]}, 1, 1
        self.__class__.chat_calls += 1
        return {"role": "assistant", "content": "你好呀"}, 1, 1


class SendProvider(FakeProvider):
    def generate(self, _messages, tools=None):
        if tools and tools[0]["function"]["name"] == "propose_proactive":
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "send", "type": "function", "function": {
                    "name": "propose_proactive",
                    "arguments": json.dumps({"reason": "有待跟进事项", "text": "面试怎么样？"}),
                },
            }]}, 1, 1
        return super().generate(_messages, tools)


class FakeEvent:
    def __init__(self, message_id, text, raw_text=None):
        self.unified_msg_origin = "qq_official:FriendMessage:test-openid"
        self.message_str = text
        self.message_obj = SimpleNamespace(
            message_id=message_id, timestamp=1795000000,
            message_str=text if raw_text is None else raw_text,
        )
        self.stopped = False

    def plain_result(self, text):
        return text

    def stop_event(self):
        self.stopped = True


class FakeContext:
    def __init__(self):
        self.sent = []

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain.chain[0].text))
        return True


@unittest.skipIf(plugin_module is None, "AstrBot is not installed in this environment")
class AstrBotPluginTests(unittest.TestCase):
    def make_root(self, directory):
        root = Path(directory)
        (root / "configs").mkdir()
        (root / "configs/character.json").write_text(
            '{"character_id":"mira","display_name":"Mira",'
            '"personality":"温柔","speaking_style":"自然",'
            '"background":"虚构角色","boundaries":"尊重用户"}',
            encoding="utf-8",
        )
        (root / ".env").write_text(
            "MIRA_MODEL_BASE_URL=https://example.test/v1\n"
            "MIRA_MODEL_API_KEY=test\nMIRA_MODEL_NAME=fake\n", encoding="utf-8",
        )
        return root

    def test_private_chat_uses_mira_once_and_controls_proactive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            FakeProvider.chat_calls = 0
            with patch.object(plugin_module, "OpenAICompatibleProvider", FakeProvider):
                plugin = plugin_module.MiraQQPlugin(None, {
                    "mira_root": str(root),
                    "target_umo": "qq_official:FriendMessage:test-openid",
                })

                async def run():
                    try:
                        first = FakeEvent("message-1", "你好")
                        first_results = [item async for item in plugin.on_private_message(first)]
                        repeated = FakeEvent("message-1", "你好")
                        repeated_results = [item async for item in plugin.on_private_message(repeated)]
                        enabled = FakeEvent("message-2", "mira on", "/mira on")
                        enabled_results = [item async for item in plugin.on_private_message(enabled)]
                        ordinary = FakeEvent("message-3", "mira status")
                        ordinary_results = [item async for item in plugin.on_private_message(ordinary)]
                        status = FakeEvent("message-4", "mira status", "/mira status")
                        status_results = [item async for item in plugin.on_private_message(status)]
                        return (first, first_results, repeated, repeated_results,
                                enabled, enabled_results, ordinary, ordinary_results,
                                status, status_results)
                    finally:
                        await plugin.terminate()

                (first, first_results, repeated, repeated_results, enabled,
                 enabled_results, ordinary, ordinary_results, status,
                 status_results) = asyncio.run(run())
            self.assertEqual(first_results, ["你好呀"])
            self.assertEqual(repeated_results, [])
            self.assertTrue(all(event.stopped for event in
                                (first, repeated, enabled, ordinary, status)))
            self.assertIn("主动联系：开启", enabled_results[0])
            self.assertEqual(ordinary_results, ["你好呀"])
            self.assertEqual(status_results, ["主动联系：开启；暂停：否"])
            self.assertEqual(FakeProvider.chat_calls, 2)
            repository = ConversationRepository(load_settings(root).database_path)
            try:
                self.assertEqual(repository.connection.execute(
                    "SELECT COUNT(*) FROM turns"
                ).fetchone()[0], 2)
                self.assertTrue(repository.proactive_enabled("local-user", "mira"))
            finally:
                repository.close()

    def test_proactive_message_uses_astrbot_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            settings = load_settings(root)
            context = FakeContext()
            with patch.object(plugin_module, "OpenAICompatibleProvider", SendProvider):
                plugin = plugin_module.MiraQQPlugin(context, {
                    "mira_root": str(root),
                    "target_umo": "qq_official:FriendMessage:test-openid",
                })
                now = datetime.now().astimezone().replace(
                    hour=11, minute=0, second=0, microsecond=0
                )
                repository = ConversationRepository(settings.database_path)
                try:
                    service = ConversationService(settings, repository, SendProvider(settings))
                    service.handle_message(MessageInput(
                        "local-user", "mira", "test", "old-message", "明天面试",
                        (now - timedelta(days=1)).isoformat(),
                    ))
                    repository.set_proactive_enabled("local-user", "mira", True)
                    turn = repository.latest_turn("local-user", "mira")
                    repository.add_follow_up(
                        turn, "问问面试结果",
                        (now - timedelta(hours=1)).astimezone(timezone.utc).isoformat(),
                        (now + timedelta(days=1)).astimezone(timezone.utc).isoformat(),
                    )
                finally:
                    repository.close()

                async def run():
                    loop = asyncio.get_running_loop()

                    def check():
                        repo = plugin._repository(settings, {"character_id": "mira"})
                        try:
                            proactive = plugin._proactive_service(settings, repo, loop)
                            proactive.clock = lambda: now
                            return proactive.check("local-user", "mira", now)
                        finally:
                            repo.close()

                    return await asyncio.to_thread(check)

                self.assertEqual(asyncio.run(run()), "sent")
            self.assertEqual(context.sent, [
                ("qq_official:FriendMessage:test-openid", "面试怎么样？")
            ])

    def test_proactive_commands_show_chinese_results(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin = plugin_module.MiraQQPlugin(None, {
                "mira_root": str(self.make_root(directory)),
                "target_umo": "qq_official:FriendMessage:test-openid",
            })
            with patch.object(plugin_module.ProactiveService, "check",
                              side_effect=["recent_conversation", "delivery_unknown"]), \
                 patch.object(plugin_module.ProactiveService, "preview",
                              return_value=("no_conversation", None, None)):
                self.assertEqual(
                    plugin._proactive_command("check", None),
                    "主动联系检查：最近六小时聊过天，暂不主动联系",
                )
                self.assertEqual(
                    plugin._proactive_command("check", None),
                    "主动联系检查：发送结果不确定，请查看聊天记录；本次不会自动重发",
                )
                self.assertEqual(
                    plugin._proactive_command("preview", None),
                    "预览（未发送）：暂无可参考的对话",
                )

    def test_qq_outbound_test_uses_proactive_delivery_path(self):
        context = FakeContext()
        plugin = plugin_module.MiraQQPlugin(context, {
            "mira_root": "D:/mira-test",
            "target_umo": "qq_official:FriendMessage:test-openid",
        })
        result = asyncio.run(plugin._command("/mira test"))
        self.assertIn("已提交", result)
        self.assertEqual(context.sent, [
            ("qq_official:FriendMessage:test-openid", plugin_module.QQ_SEND_TEST_MESSAGE)
        ])
