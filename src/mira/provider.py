import json
import socket
import urllib.error
import urllib.parse
import urllib.request

from .models import MiraError


class OpenAICompatibleProvider:
    def __init__(self, settings):
        self.settings = settings

    def generate(self, messages, tools=None, max_output_tokens=None):
        s = self.settings
        body = {'model': s.model, 'messages': messages, 'temperature': s.temperature,
                'max_tokens': max_output_tokens if max_output_tokens is not None
                else s.max_output_tokens}
        if tools:
            body['tools'] = tools
            body['tool_choice'] = 'auto'
        host = urllib.parse.urlparse(s.base_url).hostname or ''
        if host == 'api.deepseek.com' and s.model == 'deepseek-flash':
            body['thinking'] = {'type': 'disabled'}
        payload = json.dumps(body, ensure_ascii=False).encode('utf-8')
        request = urllib.request.Request(s.base_url.rstrip('/') + '/chat/completions', data=payload,
            headers={'Authorization': 'Bearer ' + s.api_key, 'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=s.timeout) as response:
                data = json.load(response)
            choice = data['choices'][0]
            if choice.get('finish_reason') == 'length':
                raise MiraError('incomplete_response')
            message = choice['message']
            content = message.get('content')
            calls = message.get('tool_calls') or []
            if calls:
                if not isinstance(calls, list) or any(
                    not isinstance(call, dict) or not isinstance(call.get('id'), str) or
                    call.get('type') != 'function' or
                    not isinstance(call.get('function'), dict) or
                    not isinstance(call['function'].get('name'), str) or
                    not isinstance(call['function'].get('arguments'), str)
                    for call in calls
                ):
                    raise MiraError('invalid_provider_response')
            elif not isinstance(content, str) or not content.strip():
                raise MiraError('empty_response')
            assistant = {'role': 'assistant', 'content': content}
            if calls:
                assistant['tool_calls'] = calls
            if message.get('reasoning_content') is not None:
                if not isinstance(message['reasoning_content'], str):
                    raise MiraError('invalid_provider_response')
                assistant['reasoning_content'] = message['reasoning_content']
            usage = data.get('usage') or {}
            return assistant, usage.get('prompt_tokens'), usage.get('completion_tokens')
        except urllib.error.HTTPError as exc:
            code = 'rate_limited' if exc.code == 429 else ('context_or_request_error' if exc.code == 400 else 'provider_http_error')
            raise MiraError(code) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise MiraError('provider_timeout') from exc
        except urllib.error.URLError as exc:
            raise MiraError('provider_connection_error') from exc
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            raise MiraError('invalid_provider_response') from exc
