import copy
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from mira.config import Settings
from mira.conversation import ConversationService
from mira.models import MessageInput, MiraError
from mira.storage import ConversationRepository


class FakeProvider:
    def __init__(self):
        self.calls = []
        self.error = None
        self.reply = None
        self.responses = []
        self.tools = []

    def generate(self, messages, tools=None):
        self.calls.append(copy.deepcopy(messages))
        self.tools.append(tools)
        if self.error:
            raise MiraError(self.error)
        if self.responses:
            return self.responses.pop(0)
        reply = self.reply if self.reply is not None else "回复 " + messages[-1]["content"]
        return {"role": "assistant", "content": reply}, 12, 4


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.character_path = root / "character.json"
        self.character_path.write_text(
            '{"character_id":"mira","display_name":"Mira",'
            '"personality":"温柔","speaking_style":"自然",'
            '"background":"虚构角色","boundaries":"尊重用户"}', encoding="utf-8"
        )
        self.settings = Settings("https://example.test/v1", "test", "fake",
                                 str(root / "mira.sqlite3"), str(self.character_path))
        self.provider = FakeProvider()
        self.repository = ConversationRepository(self.settings.database_path)
        self.service = ConversationService(self.settings, self.repository, self.provider)

    def tearDown(self):
        self.repository.close()
        self.temp.cleanup()

    def message(self, key, text):
        return MessageInput("user", "mira", "test", key, text,
                            datetime.now(timezone.utc).isoformat())

    def test_multiturn_restart_and_idempotency(self):
        first = self.message("1", "你好")
        result = self.service.handle_message(first)
        self.assertEqual(result.status, "completed")
        self.assertEqual(self.service.handle_message(first), result)
        self.assertEqual(self.service.handle_message(replace(
            first, occurred_at="2026-11-18T10:00:00+00:00")), result)
        self.assertEqual(len(self.provider.calls), 1)
        with self.assertRaisesRegex(MiraError, "message_conflict"):
            self.service.handle_message(replace(first, text="不同内容"))
        self.repository.close()
        self.repository = ConversationRepository(self.settings.database_path)
        self.service = ConversationService(self.settings, self.repository, self.provider)
        self.service.recover()
        second = self.service.handle_message(self.message("2", "继续"))
        self.assertEqual(second.status, "completed")
        self.assertEqual([m["role"] for m in self.provider.calls[-1]],
                         ["system", "user", "assistant", "user"])
        self.assertEqual(self.provider.calls[-1][-1]["content"], "继续")
        self.assertEqual(self.repository.connection.execute(
            "SELECT COUNT(*) FROM model_calls WHERE turn_id=?", (result.turn_id,)
        ).fetchone()[0], 1)

    def test_failure_retry_and_old_retry_rejected(self):
        self.provider.error = "provider_timeout"
        first = self.message("1", "一")
        failed = self.service.handle_message(first)
        self.assertEqual(failed.status, "failed")
        self.assertEqual(self.service.handle_message(first), failed)
        self.assertEqual(len(self.provider.calls), 1)
        self.provider.error = None
        retried = self.service.retry_turn(failed.turn_id, "user", "mira")
        self.assertEqual(retried.status, "completed")
        self.assertEqual(self.repository.connection.execute(
            "SELECT MAX(attempt) FROM model_calls WHERE turn_id=?", (failed.turn_id,)
        ).fetchone()[0], 2)
        self.provider.error = "rate_limited"
        old = self.service.handle_message(self.message("2", "二"))
        self.provider.error = None
        self.service.handle_message(self.message("3", "三"))
        with self.assertRaisesRegex(MiraError, "retry_not_allowed"):
            self.service.retry_turn(old.turn_id, "user", "mira")
        self.assertEqual(len(self.repository.history_before(
            self.repository.get_turn(old.turn_id), 10)), 1)

    def test_recovery_and_context_budget(self):
        message = self.message("1", "保留原文")
        self.service._character()
        turn, _ = self.repository.create_or_get(message)
        self.service.recover()
        self.assertEqual(self.repository.get_turn(turn["id"])["error_code"], "interrupted")
        self.service.settings = replace(self.settings, char_budget=1)
        failed = self.service.retry_turn(turn["id"], "user", "mira")
        self.assertEqual(failed.error_code, "context_too_large")
        self.assertEqual(self.repository.user_message(turn["id"])["text"], "保留原文")
        self.assertEqual(len(self.provider.calls), 0)

    def test_character_change_and_validation(self):
        first = self.service.handle_message(self.message("1", "你好"))
        self.character_path.write_text(
            self.character_path.read_text(encoding="utf-8").replace("温柔", "活泼"),
            encoding="utf-8",
        )
        self.service.handle_message(self.message("2", "继续"))
        self.assertIn("活泼", self.provider.calls[-1][0]["content"])
        self.character_path.write_text(
            self.character_path.read_text(encoding="utf-8").replace('"mira"', '"other"'),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(MiraError, "character_id_mismatch"):
            self.service.handle_message(self.message("3", "继续"))
        self.assertEqual(self.repository.get_turn(first.turn_id)["status"], "completed")

    def test_save_failure_does_not_report_success(self):
        original = self.repository.finish

        def fail(*args, **kwargs):
            raise sqlite3.OperationalError("simulated disk failure")

        self.repository.finish = fail
        with self.assertRaisesRegex(MiraError, "storage_error"):
            self.service.handle_message(self.message("1", "你好"))
        self.repository.finish = original
        self.assertEqual(self.service.latest_turn("user", "mira")["status"], "pending")
        self.service.recover()
        self.assertEqual(self.service.latest_turn("user", "mira")["error_code"], "interrupted")
        self.assertEqual(self.repository.connection.execute(
            "SELECT error_code FROM model_calls WHERE turn_id=?",
            (self.service.latest_turn("user", "mira")["id"],),
        ).fetchone()[0], "interrupted")

    def test_empty_reply_and_invalid_character(self):
        self.provider.reply = "  "
        failed = self.service.handle_message(self.message("1", "你好"))
        self.assertEqual(failed.error_code, "empty_response")
        self.character_path.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(MiraError, "invalid_character"):
            self.service.handle_message(self.message("2", "继续"))
        self.assertEqual(self.service.latest_turn("user", "mira")["id"], failed.turn_id)

    def test_preferences_and_corrections_survive_restart(self):
        nickname = self.service.set_preference("user", "mira", "nickname", "小墨")
        self.service.set_preference("user", "mira", "reply_style", "简短一些")
        self.service.set_preference("user", "mira", "avoid_topic", "考研")
        correction = self.service.set_preference(
            "user", "mira", "correction", "面试改为下午四点", "面试在下午三点"
        )
        self.assertEqual(len(self.service.list_preferences("user", "mira")), 4)
        self.assertEqual(self.service.list_preferences("other-user", "mira"), [])
        self.service.handle_message(self.message("1", "我几点面试？"))
        system = self.provider.calls[-1][0]["content"]
        self.assertIn("用户希望被称为：小墨", system)
        self.assertIn("用户希望的回复方式：简短一些", system)
        self.assertIn("用户不愿讨论的话题：考研", system)
        self.assertIn("当前以「面试改为下午四点」为准", system)
        self.repository.close()
        self.repository = ConversationRepository(self.settings.database_path)
        self.service = ConversationService(self.settings, self.repository, self.provider)
        self.assertEqual(len(self.service.list_preferences("user", "mira")), 4)
        self.service.remove_preference("user", "mira", correction["id"])
        self.service.remove_preference("user", "mira", nickname["id"])
        self.service.handle_message(self.message("2", "继续"))
        self.assertNotIn("用户已更正旧说法", self.provider.calls[-1][0]["content"])
        self.assertNotIn("用户希望被称为：小墨", self.provider.calls[-1][0]["content"])
        with self.assertRaisesRegex(MiraError, "preference_not_found"):
            self.service.remove_preference("user", "mira", correction["id"])

    def test_existing_database_gains_preference_table_without_losing_turns(self):
        first = self.service.handle_message(self.message("1", "原有对话"))
        with self.repository.connection:
            self.repository.connection.execute("DROP TABLE user_preferences")
            self.repository.connection.execute("DELETE FROM schema_migrations WHERE version=2")
        self.repository.close()
        self.repository = ConversationRepository(self.settings.database_path)
        self.service = ConversationService(self.settings, self.repository, self.provider)
        self.assertEqual(self.repository.get_turn(first.turn_id)["status"], "completed")
        self.assertEqual(self.service.list_preferences("user", "mira"), [])
        self.assertEqual(self.repository.connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version=2"
        ).fetchone()[0], 1)

    def test_model_calls_preference_tool_then_writes_final_reply(self):
        call = {"id": "call-name", "type": "function", "function": {
            "name": "set_preference", "arguments": '{"kind":"nickname","value":"小墨"}'}}
        self.provider.responses = [
            ({"role": "assistant", "content": None, "tool_calls": [call]}, 12, 3),
            ({"role": "assistant", "content": "好的，以后叫你小墨。"}, 20, 5),
        ]
        first = self.message("1", "以后叫我小墨。")
        result = self.service.handle_message(first)
        self.assertEqual(result.text, "好的，以后叫你小墨。")
        self.assertEqual(self.service.list_preferences("user", "mira")[0]["value"], "小墨")
        self.assertEqual(self.provider.calls[-1][-1]["role"], "tool")
        self.assertIn('"saved": true', self.provider.calls[-1][-1]["content"])
        self.assertEqual(self.service.handle_message(first), result)
        self.assertEqual(len(self.provider.calls), 2)

    def test_contact_pause_survives_restart_and_expires(self):
        pause_call = {"id": "pause", "type": "function", "function": {
            "name": "pause_contact", "arguments": '{"period":"this_week"}'}}
        self.provider.responses = [
            ({"role": "assistant", "content": None, "tool_calls": [pause_call]}, 12, 3),
            ({"role": "assistant", "content": "这周我等你来找我。"}, 20, 5),
        ]
        result = self.service.handle_message(self.message("1", "这周别主动找我"))
        self.assertEqual(result.status, "completed")
        self.assertTrue(self.service.is_contact_paused("user", "mira"))
        self.assertFalse(self.service.is_contact_paused("other-user", "mira"))
        pause = self.repository.get_contact_pause("user", "mira", datetime.now(timezone.utc))
        until = datetime.fromisoformat(pause["paused_until"])
        self.assertEqual(until.astimezone().weekday(), 0)
        self.assertEqual(until.astimezone().hour, 0)
        self.assertFalse(self.service.is_contact_paused("user", "mira", until))
        self.repository.close()
        self.repository = ConversationRepository(self.settings.database_path)
        self.service = ConversationService(self.settings, self.repository, self.provider)
        self.assertTrue(self.service.is_contact_paused("user", "mira"))
        self.service.handle_message(self.message("2", "现在聊两句"))
        self.assertIn("当前主动联系已暂停", self.provider.calls[-1][0]["content"])
        resume_call = {"id": "resume", "type": "function", "function": {
            "name": "resume_contact", "arguments": "{}"}}
        self.provider.responses = [
            ({"role": "assistant", "content": None, "tool_calls": [resume_call]}, 12, 3),
            ({"role": "assistant", "content": "已恢复。"}, 20, 5),
        ]
        self.service.handle_message(self.message("3", "可以恢复主动联系了"))
        self.assertFalse(self.service.is_contact_paused("user", "mira"))

    def test_cross_day_history_has_message_times(self):
        first = replace(self.message("1", "今天下午三点面试"),
                        occurred_at="2026-11-18T15:00:00+08:00")
        second = replace(self.message("2", "下午还有事"),
                         occurred_at="2026-11-19T09:00:00+08:00")
        self.service.handle_message(first)
        self.service.handle_message(second)
        messages = self.provider.calls[-1]
        self.assertIn("当前消息时间：", messages[0]["content"])
        self.assertIn("2026-11-19", messages[0]["content"])
        self.assertIn("2026-11-18", messages[1]["content"])
        self.assertIn("今天下午三点面试", messages[1]["content"])
        self.assertIn("不要把不同日期的安排直接当作同一件事", messages[0]["content"])

    def test_history_tool_requires_model_call_and_model_final_reply(self):
        self.service.handle_message(self.message("1", "你好"))
        self.service.handle_message(self.message("2", "今天下午三点面试"))
        call = {"id": "call-history", "type": "function", "function": {
            "name": "get_recent_user_messages", "arguments": "{}"}}
        self.provider.responses = [
            ({"role": "assistant", "content": None, "tool_calls": [call]}, 13, 2),
            ({"role": "assistant", "content": "你之前说了你好和下午三点面试。"}, 24, 8),
        ]
        question = self.message("3", "能回顾一下之前聊天吗？")
        result = self.service.handle_message(question)
        self.assertEqual(result.text, "你之前说了你好和下午三点面试。")
        tool_result = self.provider.calls[-1][-1]
        self.assertEqual(tool_result["role"], "tool")
        self.assertIn("今天下午三点面试", tool_result["content"])
        self.assertIn('"covers_all_saved_messages_in_current_database": true',
                      tool_result["content"])
        self.assertNotIn("能回顾一下之前聊天吗", tool_result["content"])
        self.assertNotIn("/exit", tool_result["content"])
        self.assertEqual(len(self.provider.calls), 4)
        self.assertEqual(self.repository.connection.execute(
            "SELECT COUNT(*) FROM model_calls WHERE turn_id=?", (result.turn_id,)
        ).fetchone()[0], 2)
        self.assertEqual(self.service.handle_message(question).text, result.text)

    def test_invalid_model_tool_arguments_do_not_change_preferences(self):
        call = {"id": "call-invalid", "type": "function", "function": {
            "name": "set_preference", "arguments": '{"kind":"nickname","value":123}'}}
        self.provider.responses = [
            ({"role": "assistant", "content": None, "tool_calls": [call]}, 10, 2),
            ({"role": "assistant", "content": "我没能保存这条称呼。"}, 15, 5),
        ]
        result = self.service.handle_message(self.message("1", "以后叫我小墨"))
        self.assertEqual(result.text, "我没能保存这条称呼。")
        self.assertEqual(self.service.list_preferences("user", "mira"), [])
        self.assertIn("invalid_tool_arguments", self.provider.calls[-1][-1]["content"])

    def test_tool_round_limit_fails_without_local_reply(self):
        self.provider.responses = [
            ({"role": "assistant", "content": None, "tool_calls": [{
                "id": f"call-{index}", "type": "function", "function": {
                    "name": "get_recent_user_messages", "arguments": "{}"
                },
            }]}, 10, 2)
            for index in range(4)
        ]
        result = self.service.handle_message(self.message("1", "聊聊之前的事"))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "tool_round_limit")
        self.assertIsNone(self.repository.assistant_message(result.turn_id))
        self.assertEqual(len(self.provider.calls), 4)

    def test_visible_preference_position_survives_database_id_gap(self):
        self.service.set_preference("user", "mira", "nickname", "小墨")
        style = self.service.set_preference("user", "mira", "reply_style", "简短")
        self.service.remove_preference("user", "mira", style["id"])
        correction = self.service.set_preference(
            "user", "mira", "correction", "测试代号是蓝色", "测试代号是红色"
        )
        self.assertEqual(correction["id"], 3)
        list_call = {"id": "call-list", "type": "function", "function": {
            "name": "list_preferences", "arguments": "{}"}}
        self.provider.responses = [
            ({"role": "assistant", "content": None, "tool_calls": [list_call]}, 10, 2),
            ({"role": "assistant", "content": "1. 小墨；2. 蓝色更正。"}, 15, 5),
        ]
        self.service.handle_message(self.message("1", "/preferences"))
        listed = json.loads(self.provider.calls[-1][-1]["content"])["preferences"]
        self.assertEqual([item["position"] for item in listed], [1, 2])
        self.assertEqual([item["kind"] for item in listed], ["nickname", "correction"])
        remove_call = {"id": "call-remove", "type": "function", "function": {
            "name": "remove_preference", "arguments": '{"position":2}'}}
        self.provider.responses = [
            ({"role": "assistant", "content": None, "tool_calls": [remove_call]}, 10, 2),
            ({"role": "assistant", "content": "已撤销第二条更正。"}, 15, 5),
        ]
        result = self.service.handle_message(self.message("2", "/remove 2"))
        self.assertEqual(result.text, "已撤销第二条更正。")
        self.assertEqual([row["kind"] for row in self.service.list_preferences("user", "mira")],
                         ["nickname"])


if __name__ == "__main__":
    unittest.main()
