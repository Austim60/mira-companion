"""Channel-independent orchestration of one conversation turn."""

import json
import sqlite3
from datetime import datetime

from .config import load_character
from .context import build_context, local_time
from .model_tools import CHAT_TOOLS
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

    def list_preferences(self, user_id, character_id):
        self._check_character_id(character_id)
        try:
            return self.repository.list_preferences(user_id, character_id)
        except sqlite3.Error as exc:
            raise MiraError("storage_error") from exc

    def set_preference(self, user_id, character_id, kind, value, key=None, source="cli"):
        """Save one explicit user rule for the current character."""
        self._check_character_id(character_id)
        if kind not in {"nickname", "reply_style", "avoid_topic", "correction"}:
            raise MiraError("invalid_preference")
        value = value.strip()
        key = key.strip() if key is not None else (kind if kind in {"nickname", "reply_style"} else value)
        if not value or not key or len(value) > 200 or len(key) > 200:
            raise MiraError("invalid_preference")
        try:
            return self.repository.set_preference(
                user_id, character_id, kind, key, value, source
            )
        except sqlite3.Error as exc:
            raise MiraError("storage_error") from exc

    def remove_preference(self, user_id, character_id, preference_id):
        self._check_character_id(character_id)
        try:
            self.repository.remove_preference(user_id, character_id, preference_id)
        except sqlite3.Error as exc:
            raise MiraError("storage_error") from exc

    def _check_character_id(self, character_id):
        character = self.prepare()
        if character_id != character["character_id"]:
            raise MiraError("character_id_mismatch")

    def _result(self, turn):
        text = None
        if turn["status"] == "completed":
            text = self.repository.assistant_message(turn["id"])["text"]
        return ConversationResult(turn["id"], turn["status"], text,
                                  turn["error_code"], turn["status"] == "failed")

    def _execute_tool(self, turn, call):
        try:
            arguments = json.loads(call["function"]["arguments"])
            if not isinstance(arguments, dict):
                raise MiraError("invalid_tool_arguments")
            name = call["function"]["name"]
            if name == "get_recent_user_messages":
                if arguments:
                    raise MiraError("invalid_tool_arguments")
                total, rows = self.repository.user_messages_before(turn, 20)
                return {"total": total, "older_count": total - len(rows),
                        "covers_all_saved_messages_in_current_database": total == len(rows),
                        "messages": [
                    {"occurred_at": local_time(row["occurred_at"]),
                     "text": row["text"][:500], "truncated": len(row["text"]) > 500}
                    for row in rows
                ]}
            if name == "list_preferences":
                if arguments:
                    raise MiraError("invalid_tool_arguments")
                return {"preferences": [
                    {"position": position, "kind": row["kind"],
                     "key": row["preference_key"], "value": row["value"]}
                    for position, row in enumerate(
                        self.repository.list_preferences(turn["user_id"], turn["character_id"]), 1
                    )
                ]}
            if name == "set_preference":
                if set(arguments) - {"kind", "value", "key"}:
                    raise MiraError("invalid_tool_arguments")
                kind, value, key = arguments.get("kind"), arguments.get("value"), arguments.get("key")
                if (not isinstance(kind, str) or not isinstance(value, str) or
                    (key is not None and not isinstance(key, str)) or
                    (kind == "correction") != (key is not None)):
                    raise MiraError("invalid_tool_arguments")
                row = self.set_preference(turn["user_id"], turn["character_id"],
                                          kind, value, key, source="model:" + turn["id"])
                return {"saved": True, "kind": row["kind"], "value": row["value"]}
            if name == "remove_preference":
                if (set(arguments) != {"position"} or type(arguments["position"]) is not int or
                    arguments["position"] < 1):
                    raise MiraError("invalid_tool_arguments")
                removed = self.repository.remove_preference_at(
                    turn["user_id"], turn["character_id"], arguments["position"]
                )
                return {"removed": True, "position": arguments["position"],
                        "kind": removed["kind"], "value": removed["value"]}
            raise MiraError("unknown_tool")
        except (ValueError, TypeError):
            return {"error": "invalid_tool_arguments"}
        except MiraError as exc:
            return {"error": exc.code}

    def _generate(self, turn, character):
        turn_id = turn["id"]
        try:
            history = self.repository.history_before(turn, self.settings.history_turns)
            user_message = self.repository.user_message(turn_id)
            preferences = self.repository.list_preferences(turn["user_id"], turn["character_id"])
            messages = build_context(character, history, user_message["text"],
                                     user_message["occurred_at"], self.settings.char_budget,
                                     preferences)
        except MiraError as exc:
            self.repository.mark_failed(turn_id, exc.code)
            return self._result(self.repository.get_turn(turn_id))
        for _ in range(4):
            attempt = self.repository.begin_attempt(turn_id, "openai_compatible", self.settings.model)
            try:
                assistant, input_tokens, output_tokens = self.provider.generate(messages, CHAT_TOOLS)
            except MiraError as exc:
                self.repository.finish(turn_id, "failed", "openai_compatible",
                                       self.settings.model, attempt, error_code=exc.code)
                return self._result(self.repository.get_turn(turn_id))
            calls = assistant.get("tool_calls") or []
            if calls:
                self.repository.complete_attempt(turn_id, attempt, input_tokens, output_tokens)
                messages.append(assistant)
                try:
                    for call in calls:
                        result = self._execute_tool(turn, call)
                        messages.append({"role": "tool", "tool_call_id": call["id"],
                                         "content": json.dumps(result, ensure_ascii=False)})
                except sqlite3.Error:
                    self.repository.mark_failed(turn_id, "storage_error")
                    return self._result(self.repository.get_turn(turn_id))
                continue
            text = assistant["content"]
            if not isinstance(text, str) or not text.strip():
                self.repository.finish(turn_id, "failed", "openai_compatible",
                                       self.settings.model, attempt, error_code="empty_response")
                return self._result(self.repository.get_turn(turn_id))
            self.repository.finish(turn_id, "completed", "openai_compatible",
                                   self.settings.model, attempt, text=text,
                                   input_tokens=input_tokens, output_tokens=output_tokens)
            return ConversationResult(turn_id, "completed", text)
        self.repository.mark_failed(turn_id, "tool_round_limit")
        return self._result(self.repository.get_turn(turn_id))
