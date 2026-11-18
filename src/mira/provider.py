import json
import socket
import urllib.error
import urllib.request

from .models import MiraError


class OpenAICompatibleProvider:
    def __init__(self, settings):
        self.settings = settings

    def generate(self, messages):
        s = self.settings
        payload = json.dumps({'model': s.model, 'messages': messages, 'temperature': s.temperature,
                              'max_tokens': s.max_output_tokens}, ensure_ascii=False).encode('utf-8')
        request = urllib.request.Request(s.base_url.rstrip('/') + '/chat/completions', data=payload,
            headers={'Authorization': 'Bearer ' + s.api_key, 'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=s.timeout) as response:
                data = json.load(response)
            text = data['choices'][0]['message']['content']
            if not isinstance(text, str) or not text.strip():
                raise MiraError('empty_response')
            usage = data.get('usage') or {}
            return text, usage.get('prompt_tokens'), usage.get('completion_tokens')
        except urllib.error.HTTPError as exc:
            code = 'rate_limited' if exc.code == 429 else ('context_or_request_error' if exc.code == 400 else 'provider_http_error')
            raise MiraError(code) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise MiraError('provider_timeout') from exc
        except urllib.error.URLError as exc:
            raise MiraError('provider_connection_error') from exc
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise MiraError('invalid_provider_response') from exc
