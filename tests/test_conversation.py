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

    def generate(self, messages):
        self.calls.append(messages)
        if self.error:
            raise MiraError(self.error)
        return self.reply if self.reply is not None else "回复 " + messages[-1]["content"], 12, 4


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


if __name__ == "__main__":
    unittest.main()
