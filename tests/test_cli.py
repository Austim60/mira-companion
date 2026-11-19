import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from threading import Event
from unittest.mock import patch

from mira.cli import main


class CliTests(unittest.TestCase):
    def test_chat_help_status_and_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            character = root / "character.json"
            character.write_text(
                '{"character_id":"mira","display_name":"Mira",'
                '"personality":"温柔","speaking_style":"自然",'
                '"background":"虚构角色","boundaries":"尊重用户"}', encoding="utf-8"
            )
            variables = {
                "MIRA_MODEL_BASE_URL": "https://example.test/v1",
                "MIRA_MODEL_API_KEY": "test-only",
                "MIRA_MODEL_NAME": "fake",
                "MIRA_CHARACTER_PATH": str(character),
                "MIRA_DATABASE_PATH": str(root / "mira.sqlite3"),
            }
            output = io.StringIO()
            with patch.dict(os.environ, variables), patch(
                "mira.provider.OpenAICompatibleProvider.generate",
                return_value=({"role": "assistant", "content": "你好呀"}, 3, 4),
            ) as generate, patch("builtins.input", side_effect=[
                "/help", "/name", "/style", "/avoid", "/correct", "/remove",
                "/name 小墨", "/avoid 考研", "/correct 面试三点 => 面试四点",
                "/preferences", "你好", "/remove 1",
                "/preferences", "/proactive on", "/status",
                "/proactive preview", "/proactive check",
                "/proactive off", "/exit",
            ]), \
                 patch("sys.stdout", output), patch(
                     "mira.cli.ProactiveService.preview",
                     return_value=("send", "预览消息", "有话题"),
                 ):
                self.assertEqual(main(["chat"]), 0)
            self.assertIn("Mira：你好呀", output.getvalue())
            self.assertIn("最近轮次：completed", output.getvalue())
            self.assertIn("主动联系授权：是", output.getvalue())
            self.assertIn("日常主动联系已关闭", output.getvalue())
            self.assertIn("主动联系预览（未发送）：预览消息", output.getvalue())
            self.assertIn("主动联系检查：最近六小时聊过天", output.getvalue())
            self.assertIn("/name\n用法：/name 小墨", output.getvalue())
            self.assertIn("/style\n用法：/style 请简短回答", output.getvalue())
            self.assertIn("用法：/name 小墨", output.getvalue())
            self.assertIn("不会修改 Mira 的名字", output.getvalue())
            self.assertIn("用法：/style 请简短回答", output.getvalue())
            self.assertIn("用法：/avoid 考研", output.getvalue())
            self.assertNotIn("未知命令", output.getvalue())
            self.assertNotIn("已保存：", output.getvalue())
            self.assertEqual(generate.call_count, 7)
            self.assertTrue((root / "mira.sqlite3").exists())
            with closing(sqlite3.connect(root / "mira.sqlite3")) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0], 7)
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM user_preferences"
                ).fetchone()[0], 0)

    def test_chat_starts_internal_proactive_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            character = root / "character.json"
            character.write_text(
                '{"character_id":"mira","display_name":"Mira",'
                '"personality":"温柔","speaking_style":"自然",'
                '"background":"虚构角色","boundaries":"尊重用户"}', encoding="utf-8"
            )
            variables = {
                "MIRA_MODEL_BASE_URL": "https://example.test/v1",
                "MIRA_MODEL_API_KEY": "test-only",
                "MIRA_MODEL_NAME": "fake",
                "MIRA_CHARACTER_PATH": str(character),
                "MIRA_DATABASE_PATH": str(root / "mira.sqlite3"),
            }
            output = io.StringIO()
            checked = Event()

            def wait_for_check(_prompt):
                self.assertTrue(checked.wait(2))
                return "/exit"

            with patch.dict(os.environ, variables), patch("sys.stdout", output), patch(
                "mira.provider.OpenAICompatibleProvider.generate"
            ) as generate, patch(
                "mira.cli.ProactiveService.check", side_effect=lambda *_: checked.set() or "disabled"
            ), patch("builtins.input", side_effect=wait_for_check):
                self.assertEqual(main(["chat"]), 0)
            self.assertTrue(checked.is_set())
            generate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
