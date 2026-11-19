"""Optional end-to-end check against the configured model service."""

import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from mira.config import load_settings
from mira.conversation import ConversationService
from mira.models import MessageInput
from mira.provider import OpenAICompatibleProvider
from mira.storage import ConversationRepository


@unittest.skipUnless(os.environ.get("MIRA_LIVE_TEST") == "1", "requires a configured live model")
class LiveModelTests(unittest.TestCase):
    def test_model_chooses_history_tool_and_answers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(load_settings(), database_path=str(root / "mira.sqlite3"),
                               character_path=str(Path("configs/character.example.json").resolve()),
                               history_turns=0, max_output_tokens=256)
            repository = ConversationRepository(settings.database_path)
            service = ConversationService(settings, repository, OpenAICompatibleProvider(settings))
            try:
                service.prepare()
                now = datetime.now(timezone.utc).isoformat()
                service.handle_message(MessageInput(
                    "test-user", "mira", "live-test", "first", "我今天下午三点有面试。", now
                ))
                result = service.handle_message(MessageInput(
                    "test-user", "mira", "live-test", "second",
                    "我前面提到的安排是什么？请核对已保存的原话再回答。", now
                ))
                self.assertEqual(result.status, "completed", result.error_code)
                self.assertIn("三点", result.text)
                calls = repository.connection.execute(
                    "SELECT COUNT(*) FROM model_calls WHERE turn_id=?", (result.turn_id,)
                ).fetchone()[0]
                self.assertGreaterEqual(calls, 2, "model did not call a tool")
            finally:
                repository.close()

    def test_explicit_style_request_is_saved_before_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(load_settings(), database_path=str(root / "mira.sqlite3"),
                               character_path=str(Path("configs/character.example.json").resolve()),
                               history_turns=0, max_output_tokens=256)
            repository = ConversationRepository(settings.database_path)
            service = ConversationService(settings, repository, OpenAICompatibleProvider(settings))
            try:
                service.prepare()
                result = service.handle_message(MessageInput(
                    "test-user", "mira", "live-test", "style",
                    "/style 回答尽量简短", datetime.now(timezone.utc).isoformat()
                ))
                self.assertEqual(result.status, "completed", result.error_code)
                saved = repository.connection.execute(
                    "SELECT value,active,source FROM user_preferences "
                    "WHERE user_id=? AND character_id=? AND kind=?",
                    ("test-user", "mira", "reply_style"),
                ).fetchone()
                self.assertIsNotNone(saved, result.text)
                self.assertEqual(saved["value"], "回答尽量简短")
                self.assertEqual(saved["active"], 1)
                self.assertTrue(saved["source"].startswith("model:"))
            finally:
                repository.close()

    def test_remove_uses_visible_position_after_id_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(load_settings(), database_path=str(root / "mira.sqlite3"),
                               character_path=str(Path("configs/character.example.json").resolve()),
                               history_turns=6, max_output_tokens=256)
            repository = ConversationRepository(settings.database_path)
            service = ConversationService(settings, repository, OpenAICompatibleProvider(settings))
            try:
                service.prepare()
                service.set_preference("test-user", "mira", "nickname", "小墨")
                style = service.set_preference("test-user", "mira", "reply_style", "简短")
                service.remove_preference("test-user", "mira", style["id"])
                service.set_preference("test-user", "mira", "correction",
                                       "测试代号是蓝色", "测试代号是红色")
                now = datetime.now(timezone.utc).isoformat()
                listed = service.handle_message(MessageInput(
                    "test-user", "mira", "live-test", "list", "/preferences", now
                ))
                self.assertEqual(listed.status, "completed", listed.error_code)
                removed = service.handle_message(MessageInput(
                    "test-user", "mira", "live-test", "remove", "/remove 2", now
                ))
                self.assertEqual(removed.status, "completed", removed.error_code)
                self.assertEqual(
                    [row["kind"] for row in service.list_preferences("test-user", "mira")],
                    ["nickname"], removed.text,
                )
            finally:
                repository.close()
