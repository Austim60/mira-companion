"""Optional end-to-end check against the configured model service."""

import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mira.config import load_settings
from mira.conversation import ConversationService
from mira.models import MessageInput
from mira.provider import OpenAICompatibleProvider
from mira.proactive import ProactiveService
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

    def test_explicit_contact_pause_is_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(load_settings(), database_path=str(root / "mira.sqlite3"),
                               character_path=str(Path("configs/character.example.json").resolve()),
                               max_output_tokens=256)
            repository = ConversationRepository(settings.database_path)
            service = ConversationService(settings, repository, OpenAICompatibleProvider(settings))
            try:
                service.prepare()
                result = service.handle_message(MessageInput(
                    "test-user", "mira", "live-test", "pause", "这周别主动找我。",
                    datetime.now(timezone.utc).isoformat()
                ))
                self.assertEqual(result.status, "completed", result.error_code)
                self.assertTrue(service.is_contact_paused("test-user", "mira"), result.text)
                pause = repository.get_contact_pause("test-user", "mira", datetime.now(timezone.utc))
                self.assertIsNotNone(pause["paused_until"], result.text)
                self.assertEqual(datetime.fromisoformat(pause["paused_until"]).astimezone().weekday(), 0)
            finally:
                repository.close()

    def test_proactive_model_chooses_send_or_skip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(load_settings(), database_path=str(root / "mira.sqlite3"),
                               character_path=str(Path("configs/character.example.json").resolve()),
                               max_output_tokens=256)
            repository = ConversationRepository(settings.database_path)
            try:
                service = ConversationService(settings, repository, OpenAICompatibleProvider(settings))
                service.prepare()
                now = datetime.now().astimezone().replace(
                    hour=11, minute=0, second=0, microsecond=0
                )
                turn, _ = repository.create_or_get(MessageInput(
                    "test-user", "mira", "live-test", "seed", "昨天说起了一部喜欢的电影。",
                    (now - timedelta(days=1)).isoformat(),
                ))
                attempt = repository.begin_attempt(turn["id"], "seed", "seed")
                repository.finish(turn["id"], "completed", "seed", "seed", attempt,
                                  text="那部电影让你印象最深的是什么？")
                repository.set_proactive_enabled("test-user", "mira", True)
                sent = []
                proactive = ProactiveService(
                    settings, repository, OpenAICompatibleProvider(settings), sent.append,
                    clock=lambda: now,
                )
                result = proactive.check("test-user", "mira", now)
                self.assertIn(result, {"sent", "skipped"}, result)
                self.assertEqual(len(sent), int(result == "sent"))
            finally:
                repository.close()

    def test_follow_up_created_and_cancelled_from_conversation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(load_settings(), database_path=str(root / "mira.sqlite3"),
                               character_path=str(Path("configs/character.example.json").resolve()),
                               max_output_tokens=256)
            repository = ConversationRepository(settings.database_path)
            service = ConversationService(settings, repository, OpenAICompatibleProvider(settings))
            try:
                service.prepare()
                repository.set_proactive_enabled("test-user", "mira", True)
                now = datetime.now(timezone.utc).isoformat()
                first = service.handle_message(MessageInput(
                    "test-user", "mira", "live-test", "plan",
                    "我明天下午三点有面试，面试结束后你可以问问我结果。", now,
                ))
                self.assertEqual(first.status, "completed", first.error_code)
                self.assertNotIn("我会来问", first.text)
                items = repository.list_follow_ups("test-user", "mira")
                self.assertEqual(len(items), 1, first.text)
                self.assertEqual(items[0]["source_turn_id"], first.turn_id)
                cancelled = service.handle_message(MessageInput(
                    "test-user", "mira", "live-test", "cancel",
                    "刚才说的面试取消了，不用再跟进。", now,
                ))
                self.assertEqual(cancelled.status, "completed", cancelled.error_code)
                self.assertNotIn("不会再提", cancelled.text)
                self.assertNotIn("觉得轻松", cancelled.text)
                self.assertEqual(repository.list_follow_ups("test-user", "mira"), [],
                                 cancelled.text)
            finally:
                repository.close()
