"""Channel-independent orchestration of one conversation turn."""

import sqlite3
from datetime import datetime

from .config import load_character
from .context import build_context
from .models import ConversationResult, MiraError


class ConversationService:
    """Persist input before generation and expose explicit failure and retry states."""

    def __init__(self, settings, repository, provider):
        self.settings = settings
        self.repository = repository
        self.provider = provider

    def _character(self):
        character = load_character(self.settings.character_path)
        self.repository.bind_character(character["character_id"], self.settings.character_path)
        return character

    def recover(self):
        try:
            self.repository.recover_pending()
        except sqlite3.Error as exc:
            raise MiraError("storage_error") from exc

    def prepare(self):
        """Validate the configured character and bind its stable identity."""
        try:
            return self._character()
        except sqlite3.Error as exc:
            raise MiraError("storage_error") from exc

    def handle_message(self, message):
        if not all((message.user_id.strip(), message.character_id.strip(),
                    message.source.strip(), message.source_message_id.strip(), message.text.strip())):
            raise MiraError("invalid_message")
        try:
            when = datetime.fromisoformat(message.occurred_at)
        except ValueError as exc:
            raise MiraError("invalid_message") from exc
        if when.tzinfo is None or when.utcoffset() is None:
            raise MiraError("invalid_message")
        character = self.prepare()
        if message.character_id != character["character_id"]:
            raise MiraError("character_id_mismatch")
        try:
            turn, created = self.repository.create_or_get(message)
        except sqlite3.Error as exc:
            raise MiraError("storage_error") from exc
        if not created:
            return self._result(turn)
        try:
            return self._generate(turn, character)
        except sqlite3.Error as exc:
            raise MiraError("storage_error") from exc

    def retry_turn(self, turn_id, user_id, character_id):
        character = self.prepare()
        if character_id != character["character_id"]:
            raise MiraError("character_id_mismatch")
        try:
            turn = self.repository.start_retry(turn_id, user_id, character_id)
        except sqlite3.Error as exc:
            raise MiraError("storage_error") from exc
        try:
            return self._generate(turn, character)
        except sqlite3.Error as exc:
            raise MiraError("storage_error") from exc

    def latest_turn(self, user_id, character_id):
        try:
            return self.repository.latest_turn(user_id, character_id)
        except sqlite3.Error as exc:
            raise MiraError("storage_error") from exc

    def _result(self, turn):
        text = None
        if turn["status"] == "completed":
            text = self.repository.assistant_message(turn["id"])["text"]
        return ConversationResult(turn["id"], turn["status"], text,
                                  turn["error_code"], turn["status"] == "failed")

    def _generate(self, turn, character):
        turn_id = turn["id"]
        try:
            history = self.repository.history_before(turn, self.settings.history_turns)
            current = self.repository.user_message(turn_id)["text"]
            messages = build_context(character, history, current, self.settings.char_budget)
        except MiraError as exc:
            self.repository.mark_failed(turn_id, exc.code)
            return self._result(self.repository.get_turn(turn_id))
        attempt = self.repository.begin_attempt(turn_id, "openai_compatible", self.settings.model)
        try:
            text, input_tokens, output_tokens = self.provider.generate(messages)
            if not isinstance(text, str) or not text.strip():
                raise MiraError("empty_response")
        except MiraError as exc:
            try:
                self.repository.finish(turn_id, "failed", "openai_compatible",
                                       self.settings.model, attempt, error_code=exc.code)
            except sqlite3.Error as storage_exc:
                raise MiraError("storage_error") from storage_exc
            return self._result(self.repository.get_turn(turn_id))
        try:
            self.repository.finish(turn_id, "completed", "openai_compatible",
                                   self.settings.model, attempt, text=text,
                                   input_tokens=input_tokens, output_tokens=output_tokens)
        except sqlite3.Error as exc:
            raise MiraError("storage_error") from exc
        return ConversationResult(turn_id, "completed", text)
