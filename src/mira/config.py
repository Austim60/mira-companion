import json
import os
from dataclasses import dataclass
from pathlib import Path

from .models import MiraError


@dataclass(frozen=True)
class Settings:
    base_url: str
    api_key: str
    model: str
    database_path: str = "data/mira.sqlite3"
    character_path: str = "configs/character.json"
    timeout: float = 60
    max_output_tokens: int = 512
    history_turns: int = 10
    char_budget: int = 12000
    temperature: float = 0.8


def load_settings(root=None, require_model=True):
    # A local .env supplies defaults; the process environment takes precedence.
    values = {}
    root = Path(root) if root is not None else Path.cwd()
    path = root / '.env'
    if path.exists():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            line = line.strip()
            if line and not line.startswith('#'):
                if '=' not in line:
                    raise MiraError('invalid_env_file')
                key, value = line.split('=', 1)
                values[key.strip()] = value.strip().strip('"\'')
    server_path = root / 'data' / 'server_config.json'
    if server_path.exists():
        try:
            server_data = json.loads(server_path.read_text(encoding='utf-8'))
            values.update(server_data.get('model', {}))
        except (OSError, ValueError, AttributeError, TypeError) as exc:
            raise MiraError('invalid_server_config') from exc
    values.update(os.environ)
    def get(name, default=''):
        return values.get('MIRA_' + name, default)
    try:
        database_path = Path(get('DATABASE_PATH', 'data/mira.sqlite3'))
        character_path = Path(get('CHARACTER_PATH', 'configs/character.json'))
        result = Settings(get('MODEL_BASE_URL'), get('MODEL_API_KEY'), get('MODEL_NAME'),
            str(database_path if database_path.is_absolute() else root / database_path),
            str(character_path if character_path.is_absolute() else root / character_path),
            float(get('TIMEOUT_SECONDS', '60')), int(get('MAX_OUTPUT_TOKENS', '512')),
            int(get('HISTORY_TURNS', '10')), int(get('CONTEXT_CHAR_BUDGET', '12000')),
            float(get('TEMPERATURE', '0.8')))
    except ValueError as exc:
        raise MiraError('invalid_settings') from exc
    if ((require_model and not all((result.base_url, result.api_key, result.model))) or
        (result.base_url and not result.base_url.startswith(('https://', 'http://'))) or
        result.timeout <= 0 or result.max_output_tokens <= 0 or result.history_turns < 0 or
        result.char_budget <= 0 or not 0 <= result.temperature <= 2):
        raise MiraError('invalid_settings')
    return result


def load_character(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        keys = ('character_id', 'display_name', 'personality', 'speaking_style', 'background', 'boundaries')
        if not isinstance(data, dict) or any(not isinstance(data.get(k), str) or not data[k].strip() for k in keys):
            raise ValueError('missing character fields')
        return data
    except (OSError, ValueError) as exc:
        raise MiraError('invalid_character') from exc
