import io
import os
import tempfile
import unittest
from pathlib import Path
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
                return_value=("你好呀", 3, 4),
            ), patch("builtins.input", side_effect=["/help", "你好", "/status", "/exit"]), \
                 patch("sys.stdout", output):
                self.assertEqual(main(["chat"]), 0)
            self.assertIn("Mira：你好呀", output.getvalue())
            self.assertIn("最近轮次：completed", output.getvalue())
            self.assertTrue((root / "mira.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
