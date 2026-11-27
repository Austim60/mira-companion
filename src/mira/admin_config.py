"""Local single-bot settings and administrator credentials."""

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
from threading import RLock
from uuid import uuid4

from .models import MiraError


MODEL_KEYS = {
    "base_url": "MIRA_MODEL_BASE_URL",
    "api_key": "MIRA_MODEL_API_KEY",
    "model": "MIRA_MODEL_NAME",
    "temperature": "MIRA_TEMPERATURE",
    "max_output_tokens": "MIRA_MAX_OUTPUT_TOKENS",
}


class AdminConfig:
    """Persist one bot and dashboard settings outside the source tree."""

    def __init__(self, root):
        self.path = Path(root) / "data" / "server_config.json"
        self._lock = RLock()
        self._data = self._read()

    def _read(self):
        if not self.path.exists():
            return {"bot": {}, "model": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MiraError("invalid_server_config") from exc
        if not isinstance(data, dict):
            raise MiraError("invalid_server_config")
        return data

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + "." + uuid4().hex + ".tmp")
        try:
            temporary.write_text(json.dumps(self._data, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def has_password(self):
        return bool(self._data.get("admin_password"))

    def set_password(self, password):
        if not isinstance(password, str) or not password:
            raise MiraError("invalid_admin_password")
        salt = os.urandom(16)
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                                n=2**14, r=8, p=1)
        with self._lock:
            self._data["admin_password"] = {
                "salt": base64.b64encode(salt).decode("ascii"),
                "digest": base64.b64encode(digest).decode("ascii"),
            }
            self._save()

    def verify_password(self, password):
        saved = self._data.get("admin_password")
        if not isinstance(saved, dict) or not isinstance(password, str):
            return False
        try:
            salt = base64.b64decode(saved["salt"], validate=True)
            expected = base64.b64decode(saved["digest"], validate=True)
        except (KeyError, ValueError):
            return False
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                                n=2**14, r=8, p=1)
        return hmac.compare_digest(actual, expected)

    def bot(self):
        with self._lock:
            return dict(self._data.get("bot", {}))

    def save_bot(self, app_id, secret=None, allowed_user_id=None, enabled=None,
                 replace=False):
        if (not isinstance(app_id, str) or not 4 <= len(app_id.strip()) <= 40 or
            any(char.isspace() for char in app_id)):
            raise MiraError("invalid_bot_id")
        if secret is not None and (not isinstance(secret, str) or
                                   not 8 <= len(secret) <= 256 or
                                   any(char.isspace() for char in secret)):
            raise MiraError("invalid_bot_secret")
        if allowed_user_id is not None and (not isinstance(allowed_user_id, str) or
                                            len(allowed_user_id) > 160):
            raise MiraError("invalid_user_id")
        with self._lock:
            bot = dict(self._data.get("bot", {}))
            if bot.get("app_id") and bot["app_id"] != app_id:
                if not replace:
                    raise MiraError("bot_replacement_requires_confirmation")
                bot = {}
            bot["app_id"] = app_id.strip()
            if secret is not None:
                bot["secret"] = secret
            if allowed_user_id is not None:
                bot["allowed_user_id"] = allowed_user_id.strip()
            if enabled is not None:
                bot["enabled"] = bool(enabled)
            self._data["bot"] = bot
            self._save()
            return dict(bot)

    def set_bot_enabled(self, enabled):
        with self._lock:
            bot = dict(self._data.get("bot", {}))
            if not bot.get("app_id") or not bot.get("secret"):
                raise MiraError("bot_not_configured")
            bot["enabled"] = bool(enabled)
            self._data["bot"] = bot
            self._save()

    def save_model(self, updates):
        if not isinstance(updates, dict) or set(updates) - set(MODEL_KEYS):
            raise MiraError("invalid_model_settings")
        if any(not isinstance(value, str) or len(value) > 512 for value in updates.values()):
            raise MiraError("invalid_model_settings")
        with self._lock:
            model = dict(self._data.get("model", {}))
            for key, value in updates.items():
                if value.strip():
                    model[MODEL_KEYS[key]] = value.strip()
            self._data["model"] = model
            self._save()

    def public_model(self):
        values = dict(self._data.get("model", {}))
        return {
            "base_url": values.get("MIRA_MODEL_BASE_URL", ""),
            "model": values.get("MIRA_MODEL_NAME", ""),
            "temperature": values.get("MIRA_TEMPERATURE", ""),
            "max_output_tokens": values.get("MIRA_MAX_OUTPUT_TOKENS", ""),
            "has_api_key": bool(values.get("MIRA_MODEL_API_KEY")),
        }
