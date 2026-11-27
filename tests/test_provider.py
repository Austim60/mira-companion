import io
import json
import unittest
from unittest.mock import patch

from mira.config import Settings
from mira.models import MiraError
from mira.provider import OpenAICompatibleProvider


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.provider = OpenAICompatibleProvider(Settings(
            "https://api.deepseek.com/v1", "test-only", "deepseek-flash"
        ))

    def test_tool_call_request_and_response(self):
        response = {"choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "tool_calls": [{
                "id": "call-1", "type": "function", "function": {
                    "name": "get_recent_user_messages", "arguments": "{}"
                },
            }],
        }}], "usage": {"prompt_tokens": 8, "completion_tokens": 3}}
        with patch("urllib.request.urlopen", return_value=io.BytesIO(
            json.dumps(response).encode("utf-8")
        )) as urlopen:
            message, input_tokens, output_tokens = self.provider.generate(
                [{"role": "user", "content": "回顾聊天"}],
                [{"type": "function", "function": {"name": "get_recent_user_messages"}}],
            )
        request = urlopen.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(body["tool_choice"], "auto")
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(message["tool_calls"][0]["function"]["name"], "get_recent_user_messages")
        self.assertIsNone(message["content"])
        self.assertEqual((input_tokens, output_tokens), (8, 3))

    def test_empty_final_answer_is_rejected(self):
        response = {"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": "  "
        }}]}
        with patch("urllib.request.urlopen", return_value=io.BytesIO(
            json.dumps(response).encode("utf-8")
        )):
            with self.assertRaisesRegex(MiraError, "empty_response"):
                self.provider.generate([{"role": "user", "content": "你好"}], [])

    def test_output_limit_can_be_overridden_for_memory_extraction(self):
        response = {"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": "{}"
        }}]}
        with patch("urllib.request.urlopen", return_value=io.BytesIO(
            json.dumps(response).encode("utf-8")
        )) as urlopen:
            self.provider.generate([{"role": "user", "content": "记忆"}],
                                   max_output_tokens=2048)
        self.assertEqual(json.loads(urlopen.call_args.args[0].data)["max_tokens"], 2048)
        self.assertEqual(json.loads(urlopen.call_args.args[0].data)["thinking"],
                         {"type": "disabled"})


if __name__ == "__main__":
    unittest.main()
