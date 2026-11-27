import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from qqbot_agent_sdk import QQApiClient as SDKQQApiClient

from mira.admin_config import AdminConfig
from mira.config import load_settings
from mira.models import MessageInput, MiraError
from mira.memory_storage import MemoryRepository
from mira.storage import ConversationRepository
from mira.server import create_app
from mira.server import main as server_main
from mira.standalone import EventLog, MiraRuntime


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtime = MiraRuntime(self.root)
        self.client = TestClient(create_app(self.root, self.runtime))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def login(self):
        response = self.client.post("/api/setup", json={"password": "long-test-password"})
        self.assertEqual(response.status_code, 200)
        return {"X-Mira-CSRF": response.json()["csrf"]}

    def test_setup_auth_and_single_bot_configuration(self):
        self.assertTrue(self.client.get("/api/session").json()["setup_required"])
        self.assertEqual(self.client.get("/api/state").status_code, 401)
        headers = self.login()
        self.assertEqual(self.client.get("/api/state").status_code, 200)
        self.assertEqual(self.client.post("/api/bot", json={
            "app_id": "123456", "secret": "long-secret",
        }).status_code, 403)
        self.assertEqual(self.client.post("/api/bot", headers=headers, json={
            "app_id": "123456", "secret": "long-secret", "allowed_user_id": "openid-1",
        }).status_code, 200)
        state = self.client.get("/api/state").json()
        self.assertTrue(state["bot"]["has_secret"])
        self.assertNotIn("long-secret", json.dumps(state))
        self.assertNotIn("secret", state["bot"])
        self.assertEqual(state["bot"]["allowed_user_id"], "openid-1")
        self.assertTrue(AdminConfig(self.root).verify_password("long-test-password"))
        self.assertEqual(self.client.post("/api/setup", json={
            "password": "another-password"
        }).status_code, 409)

    def test_one_character_password_can_be_set_and_used(self):
        self.assertEqual(self.client.post(
            "/api/setup", json={"password": ""}
        ).status_code, 400)
        response = self.client.post("/api/setup", json={"password": "x"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(AdminConfig(self.root).verify_password("x"))
        self.client.cookies.clear()
        self.assertEqual(self.client.post(
            "/api/login", json={"password": "x"}
        ).status_code, 200)

    def test_character_and_model_settings_are_editable(self):
        headers = self.login()
        character = {"character_id": "mira", "display_name": "Mira",
                     "personality": "温柔", "speaking_style": "简短",
                     "background": "虚构角色", "boundaries": "尊重用户"}
        response = self.client.post("/api/character", headers=headers, json=character)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get("/api/state").json()["character"], character)
        response = self.client.post("/api/model", headers=headers, json={
            "base_url": "https://example.test/v1", "model": "test-model",
            "api_key": "test-key", "temperature": "0.6",
        })
        self.assertEqual(response.status_code, 200, response.text)
        settings = load_settings(self.root)
        self.assertEqual((settings.model, settings.temperature), ("test-model", 0.6))
        self.assertNotIn("test-key", self.client.get("/api/state").text)

    def test_dashboard_cannot_read_secret_or_modify_without_login(self):
        self.login()
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/logs").status_code, 401)
        self.assertEqual(self.client.get("/api/runtime-logs").status_code, 401)
        self.assertEqual(self.client.get("/api/memories").status_code, 401)
        self.assertEqual(self.client.get("/api/conversations").status_code, 401)
        self.assertEqual(self.client.post("/api/onboard/start").status_code, 401)
        self.assertEqual(self.client.get("/api/onboard/qr").status_code, 401)

    def test_raw_history_includes_unprocessed_turns_and_pages_by_sequence(self):
        headers = self.login()
        character = {"character_id": "mira", "display_name": "Mira", "personality": "温柔",
                     "speaking_style": "自然", "background": "虚构", "boundaries": "尊重用户"}
        self.assertEqual(self.client.post("/api/character", headers=headers,
                                          json=character).status_code, 200)
        repo = ConversationRepository(load_settings(self.root, require_model=False).database_path)
        try:
            repo.bind_character("mira", "test")
            for i in range(23):
                turn, _ = repo.create_or_get(MessageInput(
                    "other" if i == 21 else "local-user", "mira", "admin" if i == 22 else "qq:1",
                    str(i), f"原话 {i}", "2026-11-26T12:00:00+00:00",
                ))
                attempt = repo.begin_attempt(turn["id"], "test", "test")
                repo.finish(turn["id"], "completed", "test", "test", attempt, text="原始回复")
        finally:
            repo.close()
        page = self.client.get("/api/conversations").json()
        self.assertEqual(len(page["items"]), 20)
        first = page["items"][0]
        self.assertEqual((first["user_text"], first["reply"], first["job_status"]),
                         ("原话 20", "原始回复", "pending"))
        last = self.client.get("/api/conversations", params={"before": page["next_before"]}).json()
        self.assertEqual([item["user_text"] for item in last["items"]], ["原话 0"])
        self.assertIsNone(last["next_before"])

    def test_memory_organization_requires_login_and_keeps_source(self):
        headers = self.login()
        character = {"character_id": "mira", "display_name": "Mira", "personality": "温柔",
                     "speaking_style": "自然", "background": "虚构", "boundaries": "尊重用户"}
        self.client.post("/api/character", headers=headers, json=character)
        repo = ConversationRepository(load_settings(self.root, require_model=False).database_path)
        try:
            turn, _ = repo.create_or_get(MessageInput(
                "local-user", "mira", "qq:1", "memory-1", "我喜欢蓝色",
                "2026-11-26T12:00:00+00:00",
            ))
            attempt = repo.begin_attempt(turn["id"], "test", "test")
            repo.finish(turn["id"], "completed", "test", "test", attempt, text="记住了")
            memory_id = MemoryRepository(repo).save_explicit(
                repo.get_turn(turn["id"]), "我喜欢蓝色", "interest"
            )
        finally:
            repo.close()
        route = f"/api/memories/{memory_id}/organize"
        body = {"category": "个人喜好", "kind": "user_fact"}
        self.assertEqual(self.client.post(route, json=body).status_code, 403)
        self.assertEqual(self.client.post(route, headers=headers, json=body).status_code, 200)
        item = self.client.get("/api/memories").json()["items"][0]
        self.assertEqual((item["category"], item["kind"]), ("个人喜好", "user_fact"))
        self.assertEqual(self.client.get(
            f"/api/memories/{memory_id}/sources"
        ).json()["sources"][0]["text"], "我喜欢蓝色")

    def test_log_details_are_authenticated_and_legacy_entries_still_load(self):
        self.runtime.events.add("qq", "QQ 连接失败", "RuntimeError")
        self.assertEqual(self.client.get("/api/logs").status_code, 401)
        self.login()
        events = self.client.get("/api/logs").json()["events"]
        legacy = next(event for event in events if event["message"] == "QQ 连接失败")
        self.assertEqual(legacy["code"], "RuntimeError")
        self.assertNotIn("detail", legacy)

    def test_chat_failure_detail_does_not_store_exception_message(self):
        try:
            raise RuntimeError("私聊原文：不要记录这句话")
        except RuntimeError as exc:
            detail = EventLog.failure_detail(exc, "chat")
        self.assertEqual(detail["stage"], "chat")
        self.assertTrue(detail["stack"])
        self.assertNotIn("不要记录这句话", json.dumps(detail, ensure_ascii=False))
        self.runtime.events.add("chat", "私聊处理失败", "RuntimeError",
                                detail, level="ERROR")
        lines = self.runtime.logs.tail()
        self.assertTrue(any("ERROR [chat] 私聊处理失败" in line for line in lines))
        self.assertNotIn("不要记录这句话", "\n".join(lines))

    def test_runtime_log_view_returns_recent_lines_and_file(self):
        self.login()
        self.runtime.logs.write("INFO", "qq", "开始获取 QQ 令牌")
        result = self.client.get("/api/runtime-logs?limit=1")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(result.json()["lines"]), 1)
        self.assertIn("开始获取 QQ 令牌", result.json()["lines"][0])
        self.assertTrue(result.json()["file"].endswith("mira.log"))
        self.assertEqual(self.client.get("/api/runtime-logs?limit=1001").status_code, 422)

    def test_unconfigured_model_does_not_fill_logs_with_worker_failures(self):
        self.runtime.run_due()
        self.assertEqual(self.runtime.events.recent(), [])

    def test_server_defaults_to_project_root(self):
        with patch("mira.server.create_app") as create, patch("mira.server.uvicorn.run"):
            server_main([])
        self.assertEqual(Path(create.call_args.args[0]),
                         Path(__file__).resolve().parents[1])


class FakeQQApi:
    instances = []
    build_text_body = staticmethod(SDKQQApiClient.build_text_body)

    def __init__(self, app_id, client_secret):
        self.sent = []
        self.http_client = None
        self.instances.append(self)

    def setup(self, http_client):
        self.http_client = http_client

    async def ensure_token(self):
        return "token"

    async def get_gateway_url(self):
        return "wss://example.test/gateway"

    def ensure_token_sync(self):
        return "token"

    def get_gateway_url_sync(self):
        return "wss://example.test/gateway"

    def clear_token(self):
        pass

    async def send_text(self, chat_type, chat_id, text, **kwargs):
        self.sent.append((chat_type, chat_id, text, kwargs))
        return {"id": "sent-1"}

    async def post_c2c_message(self, chat_id, message):
        self.sent.append(("c2c", chat_id, message.to_dict(), {}))
        return {"id": "sent-1"}


class FakeQQWebSocket:
    def __init__(self, callbacks):
        self.callbacks = callbacks

    def start(self, gateway_url, loop):
        self.callbacks.on_connected()

    async def async_stop(self):
        self.callbacks.on_disconnected()


class QQConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = MiraRuntime(self.temp.name)
        self.runtime.config.save_bot("123456", "long-secret")
        FakeQQApi.instances.clear()
        self.patches = [
            patch("mira.standalone.QQApiClient", FakeQQApi),
            patch("mira.standalone.QQWebSocket", FakeQQWebSocket),
        ]
        for active in self.patches:
            active.start()

    async def asyncTearDown(self):
        await self.runtime.qq.disconnect()
        for active in reversed(self.patches):
            active.stop()
        self.temp.cleanup()

    async def test_manual_connect_bind_receive_and_send(self):
        await self.runtime.qq.connect()
        self.assertEqual(self.runtime.qq.status, "connected")
        self.assertIsNotNone(FakeQQApi.instances[0].http_client)
        self.assertTrue(any("QQ 网关地址获取成功" in line
                            for line in self.runtime.logs.tail()))
        event = SimpleNamespace(chat_scope="c2c", content="你好", user_id="user-1",
                                user_name="小墨", chat_id="chat-1",
                                message_id="message-1", timestamp="2026-11-26T01:00:00Z")
        with patch("mira.standalone.EventParser") as parser:
            parser.return_value.parse.return_value = event
            await self.runtime.qq.on_message("C2C_MESSAGE_CREATE", {})
            self.assertEqual(self.runtime.qq.pending_user["user_id"], "user-1")
            self.runtime.config.save_bot("123456", allowed_user_id="user-1")
            with patch.object(self.runtime.qq, "_handle_message", return_value=("回复", False)):
                await self.runtime.qq.on_message("C2C_MESSAGE_CREATE", {})
        sent = FakeQQApi.instances[0].sent[0]
        self.assertEqual(sent[:2], ("c2c", "chat-1"))
        self.assertEqual(sent[2]["content"], "回复")
        self.assertEqual(sent[2]["msg_id"], "message-1")
        self.assertNotIn("message_reference", sent[2])
        with patch("mira.standalone.EventParser") as parser:
            parser.return_value.parse.return_value = event
            with patch.object(self.runtime.qq, "_handle_message", return_value=("引用回复", True)):
                await self.runtime.qq.on_message("C2C_MESSAGE_CREATE", {})
        quoted = FakeQQApi.instances[0].sent[1]
        self.assertEqual(quoted[2]["content"], "引用回复")
        self.assertEqual(quoted[2]["msg_id"], "message-1")
        self.assertEqual(quoted[2]["message_reference"]["message_id"], "message-1")
        await self.runtime.qq.send_proactive("主动问候")
        self.assertEqual(FakeQQApi.instances[0].sent[2][:3],
                         ("c2c", "user-1", "主动问候"))
        await self.runtime.qq.disconnect()
        self.assertTrue(FakeQQApi.instances[0].http_client.is_closed)

    async def test_gateway_failure_is_visible_and_closes_http_client(self):
        with patch.object(FakeQQApi, "get_gateway_url",
                          side_effect=RuntimeError("gateway unavailable: long-secret")):
            with self.assertRaises(MiraError) as raised:
                await self.runtime.qq.connect()
        self.assertEqual(raised.exception.code, "qq_gateway_failed")
        self.assertEqual(self.runtime.qq.last_error, "qq_gateway_failed")
        self.assertTrue(FakeQQApi.instances[0].http_client.is_closed)
        event = self.runtime.events.recent()[0]
        self.assertEqual(event["detail"]["stage"], "gateway")
        self.assertTrue(event["detail"]["stack"])
        self.assertIn("[已隐藏]", event["detail"]["reason"])
        self.assertNotIn("long-secret", json.dumps(event, ensure_ascii=False))

    async def test_qr_onboarding_saves_credentials_without_starting_connection(self):
        async def fake_onboard(on_qr_ready):
            on_qr_ready("https://example.test/scan")
            return SimpleNamespace(app_id="789012", client_secret="new-secret",
                                   user_openid="user-2")

        with self.assertRaises(MiraError):
            self.runtime.start_onboarding()
        self.assertEqual(self.runtime.config.bot()["app_id"], "123456")
        with patch("mira.standalone.start_onboard", fake_onboard):
            self.runtime.start_onboarding(replace=True)
            await self.runtime._onboard_task
        self.assertEqual(self.runtime.onboard["status"], "completed")
        self.assertEqual(self.runtime.config.bot()["allowed_user_id"], "user-2")
        self.assertFalse(self.runtime.config.bot()["enabled"])


if __name__ == "__main__":
    unittest.main()
