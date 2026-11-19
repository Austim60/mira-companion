import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mira.config import Settings
from mira.conversation import ConversationService
from mira.models import MessageInput
from mira.proactive import ProactiveService
from mira.storage import ConversationRepository


class FakeProvider:
    def __init__(self, name="propose_proactive", before_reply=None):
        self.name = name
        self.before_reply = before_reply
        self.calls = 0
        self.messages = None

    def generate(self, messages, tools=None):
        self.calls += 1
        self.messages = messages
        if self.before_reply:
            self.before_reply()
        if tools is None or tools[0]["function"]["name"] != "propose_proactive":
            return {"role": "assistant", "content": "好"}, 1, 1
        arguments = ({"reason": "有自然的话题" , "text": "今天过得怎么样？"}
                     if self.name == "propose_proactive" else {"reason": "现在不适合联系"})
        return {"role": "assistant", "content": None, "tool_calls": [{
            "id": "choice", "type": "function", "function": {
                "name": self.name, "arguments": json.dumps(arguments, ensure_ascii=False)
            },
        }]}, 1, 1


class ProactiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        character = root / "character.json"
        character.write_text(
            '{"character_id":"mira","display_name":"Mira",'
            '"personality":"温柔","speaking_style":"自然",'
            '"background":"虚构角色","boundaries":"尊重用户"}', encoding="utf-8"
        )
        self.settings = Settings("https://example.test/v1", "test", "fake",
                                 str(root / "mira.sqlite3"), str(character))
        self.repository = ConversationRepository(self.settings.database_path)
        self.conversation = ConversationService(
            self.settings, self.repository, FakeProvider()
        )
        self.now = datetime.now().astimezone().replace(hour=11, minute=0, second=0, microsecond=0)
        if self.now > datetime.now().astimezone():
            self.now -= timedelta(days=1)
        self.conversation.handle_message(MessageInput(
            "user", "mira", "test", "first", "最近聊了件有趣的事",
            (self.now - timedelta(days=1)).isoformat(),
        ))
        self.sent = []
        self.provider = FakeProvider()
        self.proactive = ProactiveService(
            self.settings, self.repository, self.provider, self.sent.append,
            clock=lambda: self.now,
        )

    def tearDown(self):
        self.repository.close()
        self.temp.cleanup()

    def test_opt_in_send_daily_limit_and_unanswered_backoff(self):
        self.assertEqual(self.proactive.check("user", "mira", self.now), "disabled")
        self.assertEqual(self.provider.calls, 0)
        self.repository.set_proactive_enabled("user", "mira", True)
        self.assertEqual(self.proactive.check("user", "mira", self.now), "sent")
        self.assertEqual(self.sent, ["今天过得怎么样？"])
        self.assertEqual(self.proactive.check("user", "mira", self.now), "daily_limit")
        tomorrow = self.now + timedelta(days=1)
        self.assertEqual(self.proactive.check("user", "mira", tomorrow), "awaiting_reply")
        self.assertEqual(self.provider.calls, 1)

    def test_preview_calls_model_without_authorization_or_delivery(self):
        choice, content, reason = self.proactive.preview("user", "mira")
        self.assertEqual(choice, "send")
        self.assertEqual(content, "今天过得怎么样？")
        self.assertEqual(reason, "有自然的话题")
        self.assertEqual(self.sent, [])
        self.assertFalse(self.repository.proactive_enabled("user", "mira"))
        self.assertEqual(self.repository.connection.execute(
            "SELECT COUNT(*) FROM proactive_decisions"
        ).fetchone()[0], 0)

    def test_skip_is_not_sent_and_window_is_idempotent(self):
        self.repository.set_proactive_enabled("user", "mira", True)
        self.provider.name = "skip_proactive"
        self.assertEqual(self.proactive.check("user", "mira", self.now), "skipped")
        self.assertEqual(self.proactive.check("user", "mira", self.now), "already_checked")
        self.assertEqual(self.sent, [])

    def test_pause_added_during_generation_blocks_delivery(self):
        self.repository.set_proactive_enabled("user", "mira", True)
        self.provider.before_reply = lambda: self.repository.set_contact_pause(
            "user", "mira", (self.now + timedelta(days=1)).isoformat(), "test"
        )
        self.assertEqual(self.proactive.check("user", "mira", self.now), "boundary_changed")
        self.assertEqual(self.sent, [])

    def test_chat_exit_during_generation_prevents_delivery(self):
        self.repository.set_proactive_enabled("user", "mira", True)
        stopped = [False]
        self.provider.before_reply = lambda: stopped.__setitem__(0, True)
        self.proactive.should_stop = lambda: stopped[0]
        self.assertEqual(self.proactive.check("user", "mira", self.now), "stopped")
        self.assertEqual(self.sent, [])

    def test_next_chat_knows_what_was_sent(self):
        self.repository.set_proactive_enabled("user", "mira", True)
        self.assertEqual(self.proactive.check("user", "mira", self.now), "sent")
        self.conversation.handle_message(MessageInput(
            "user", "mira", "test", "reply", "刚看到你的消息",
            (self.now + timedelta(hours=1)).isoformat(),
        ))
        self.assertIn("今天过得怎么样？", self.conversation.provider.messages[0]["content"])

    def test_due_follow_up_is_used_and_marked_asked(self):
        self.repository.set_proactive_enabled("user", "mira", True)
        turn = self.repository.latest_turn("user", "mira")
        follow_up = self.repository.add_follow_up(
            turn, "问问面试结果", (self.now - timedelta(hours=1)).astimezone(timezone.utc).isoformat(),
            (self.now + timedelta(days=1)).astimezone(timezone.utc).isoformat(),
        )
        self.assertEqual(self.proactive.check("user", "mira", self.now), "sent")
        self.assertIn("问问面试结果", self.provider.messages[0]["content"])
        self.assertIn("最近聊了件有趣的事", self.provider.messages[0]["content"])
        self.assertEqual(self.repository.connection.execute(
            "SELECT status FROM follow_ups WHERE id=?", (follow_up["id"],)
        ).fetchone()[0], "asked")

    def test_cancel_during_generation_blocks_follow_up(self):
        self.repository.set_proactive_enabled("user", "mira", True)
        turn = self.repository.latest_turn("user", "mira")
        follow_up = self.repository.add_follow_up(
            turn, "问问面试结果", (self.now - timedelta(hours=1)).astimezone(timezone.utc).isoformat(),
            (self.now + timedelta(days=1)).astimezone(timezone.utc).isoformat(),
        )
        self.provider.before_reply = lambda: self.repository.close_follow_up(
            "user", "mira", follow_up["id"], "cancelled"
        )
        self.assertEqual(self.proactive.check("user", "mira", self.now), "boundary_changed")
        self.assertEqual(self.sent, [])

    def test_unknown_delivery_does_not_repeat_follow_up(self):
        self.repository.set_proactive_enabled("user", "mira", True)
        turn = self.repository.latest_turn("user", "mira")
        follow_up = self.repository.add_follow_up(
            turn, "问问面试结果", (self.now - timedelta(hours=1)).astimezone(timezone.utc).isoformat(),
            (self.now + timedelta(days=1)).astimezone(timezone.utc).isoformat(),
        )

        def fail_delivery(_text):
            raise OSError("unknown delivery")

        self.proactive.deliver = fail_delivery
        self.assertEqual(self.proactive.check("user", "mira", self.now), "delivery_unknown")
        self.assertEqual(self.repository.connection.execute(
            "SELECT status FROM follow_ups WHERE id=?", (follow_up["id"],)
        ).fetchone()[0], "uncertain")
        self.assertEqual(self.proactive.check("user", "mira", self.now), "daily_limit")

    def test_pending_source_cannot_trigger_and_failed_source_is_removed(self):
        turn, _ = self.repository.create_or_get(MessageInput(
            "user", "mira", "test", "unfinished", "明天有面试",
            (self.now - timedelta(days=1)).isoformat(),
        ))
        follow_up = self.repository.add_follow_up(
            turn, "问问面试结果", (self.now - timedelta(hours=1)).astimezone(timezone.utc).isoformat(),
            (self.now + timedelta(days=1)).astimezone(timezone.utc).isoformat(),
        )
        self.assertIsNone(self.repository.due_follow_up("user", "mira", self.now))
        self.repository.mark_failed(turn["id"], "test_failure")
        self.assertIsNone(self.repository.connection.execute(
            "SELECT 1 FROM follow_ups WHERE id=?", (follow_up["id"],)
        ).fetchone())

    def test_one_turn_can_hold_distinct_follow_ups_without_duplicates(self):
        turn = self.repository.latest_turn("user", "mira")
        due = (self.now + timedelta(days=1)).astimezone(timezone.utc).isoformat()
        expires = (self.now + timedelta(days=2)).astimezone(timezone.utc).isoformat()
        first = self.repository.add_follow_up(turn, "问面试结果", due, expires)
        duplicate = self.repository.add_follow_up(turn, "问面试结果", due, expires)
        second = self.repository.add_follow_up(turn, "问体检结果", due, expires)
        self.assertEqual(first["id"], duplicate["id"])
        self.assertNotEqual(first["id"], second["id"])

    def test_existing_database_adds_follow_up_column(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "old.sqlite3")
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TABLE proactive_decisions ("
                "id TEXT PRIMARY KEY, user_id TEXT NOT NULL, character_id TEXT NOT NULL, "
                "opportunity TEXT NOT NULL, status TEXT NOT NULL, text TEXT, reason TEXT, "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                "UNIQUE(user_id, character_id, opportunity))"
            )
            connection.close()
            repository = ConversationRepository(path)
            try:
                columns = {row[1] for row in repository.connection.execute(
                    "PRAGMA table_info(proactive_decisions)"
                )}
                self.assertIn("follow_up_id", columns)
            finally:
                repository.close()


if __name__ == "__main__":
    unittest.main()
