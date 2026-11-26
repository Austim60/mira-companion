"""Opt-in local proactive contact with model choice and delivery gates."""

import json
from datetime import datetime, timedelta, timezone

from .config import load_character
from .models import MiraError
from .memory import MemoryService


DECISION_TOOLS = [
    {"type": "function", "function": {
        "name": "propose_proactive", "description": "当前确实值得联系时，提出一条可让用户不回复的简短消息。不得编造共同经历或现实行动。",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["text", "reason"], "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": "skip_proactive", "description": "现在没有合适理由联系，或联系可能打扰用户时跳过。",
        "parameters": {"type": "object", "properties": {"reason": {"type": "string"}},
            "required": ["reason"], "additionalProperties": False},
    }},
]


PROACTIVE_RESULT_MESSAGES = {
    "disabled": "主动联系尚未开启",
    "paused": "主动联系已暂停",
    "quiet_hours": "当前不在主动联系时段（09:00—22:00）",
    "daily_limit": "今天已达到主动联系上限",
    "recent_conversation": "最近六小时聊过天，暂不主动联系",
    "awaiting_reply": "上一条主动消息尚未得到回复，暂不重复联系",
    "no_conversation": "暂无可参考的对话",
    "already_checked": "当前时段已经检查过，不会重复检查",
    "skipped": "模型决定这次不联系",
    "sent": "已发送主动消息",
    "boundary_changed": "发送前条件发生变化，本次未发送",
    "stopped": "检查已停止，本次未发送",
    "avoided_topic": "拟发送内容涉及你不希望讨论的话题，本次未发送",
    "delivery_unknown": "发送结果不确定，请查看聊天记录；本次不会自动重发",
    "invalid_proactive_decision": "模型的判断结果无效，本次未发送",
}


def proactive_result_message(result):
    """Translate a proactive check result for user-facing commands."""
    return PROACTIVE_RESULT_MESSAGES.get(result, "检查未完成，请查看运行日志")


class ProactiveService:
    """Check a time window once, then ask the model whether to contact."""

    def __init__(self, settings, repository, provider, deliver, clock=None, should_stop=None):
        self.settings = settings
        self.repository = repository
        self.provider = provider
        self.deliver = deliver
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.should_stop = should_stop or (lambda: False)

    def preview(self, user_id, character_id):
        """Ask the model for a draft without changing contact or delivery state."""
        character = load_character(self.settings.character_path)
        self.repository.bind_character(character["character_id"], self.settings.character_path)
        if character["character_id"] != character_id:
            raise MiraError("character_id_mismatch")
        if not self.repository.recent_completed_messages(user_id, character_id, 1):
            return "no_conversation", None, None
        follow_up = self.repository.due_follow_up(
            user_id, character_id, self.clock().astimezone()
        )
        assistant, _, _ = self.provider.generate(
            self._messages(character, user_id, character_id, self.clock().astimezone(), follow_up),
            DECISION_TOOLS,
        )
        choice, content, reason = self._parse_decision(assistant)
        if choice == "send" and self._is_avoided(user_id, character_id, content):
            return "avoided_topic", None, None
        return choice, content, reason

    def check(self, user_id, character_id, now=None):
        now = now or self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise MiraError("invalid_time")
        now = now.astimezone()
        character = load_character(self.settings.character_path)
        self.repository.bind_character(character["character_id"], self.settings.character_path)
        if character["character_id"] != character_id:
            raise MiraError("character_id_mismatch")
        window = self._window(now)
        if window is None:
            return "quiet_hours"
        allowed = self._allowed(user_id, character_id, now)
        if allowed != "allowed":
            return allowed
        follow_up = self.repository.due_follow_up(user_id, character_id, now)
        opportunity = (("followup:" + follow_up["id"] + ":" if follow_up else "daily:") +
                       now.strftime("%Y-%m-%d") + ":" + window)
        if not self.repository.reserve_opportunity(
            user_id, character_id, opportunity, follow_up["id"] if follow_up else None, now
        ):
            return "already_checked"
        try:
            assistant, _, _ = self.provider.generate(
                self._messages(character, user_id, character_id, now, follow_up), DECISION_TOOLS
            )
            choice, content, reason = self._parse_decision(assistant)
        except MiraError as exc:
            self.repository.finish_opportunity(user_id, character_id, opportunity,
                                               "skipped", reason=exc.code)
            return exc.code
        if self.should_stop():
            self.repository.finish_opportunity(user_id, character_id, opportunity,
                                               "skipped", reason="stopped")
            return "stopped"
        if choice == "skip":
            self.repository.finish_opportunity(user_id, character_id, opportunity,
                                               "skipped", reason=reason)
            return "skipped"
        if self._is_avoided(user_id, character_id, content):
            self.repository.finish_opportunity(user_id, character_id, opportunity,
                                               "skipped", reason="avoided_topic")
            return "avoided_topic"
        send_time = self.clock().astimezone()
        allowed = self._allowed(user_id, character_id, send_time)
        if allowed != "allowed" or not self.repository.claim_delivery(
            user_id, character_id, opportunity, send_time.astimezone(timezone.utc),
            self._day_start(send_time), content, reason
        ):
            self.repository.finish_opportunity(user_id, character_id, opportunity,
                                               "skipped", reason="boundary_changed")
            return "boundary_changed"
        try:
            if self.should_stop():
                self.repository.finish_opportunity(user_id, character_id, opportunity,
                                                   "skipped", reason="stopped")
                return "stopped"
            self.deliver(content)
        except Exception:
            self.repository.finish_opportunity(user_id, character_id, opportunity,
                                               "unknown", content, "delivery_unknown")
            return "delivery_unknown"
        self.repository.finish_opportunity(user_id, character_id, opportunity,
                                           "sent", content, reason)
        return "sent"

    def _is_avoided(self, user_id, character_id, content):
        return any(row["kind"] == "avoid_topic" and row["value"].casefold() in content.casefold()
                   for row in self.repository.list_preferences(user_id, character_id))

    @staticmethod
    def _window(now):
        if 9 <= now.hour < 13:
            return "morning"
        if 13 <= now.hour < 18:
            return "afternoon"
        if 18 <= now.hour < 22:
            return "evening"
        return None

    @staticmethod
    def _day_start(now):
        return now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(
            timezone.utc
        ).isoformat()

    def _allowed(self, user_id, character_id, now):
        if not self.repository.proactive_enabled(user_id, character_id):
            return "disabled"
        if self.repository.get_contact_pause(user_id, character_id, now.astimezone(timezone.utc)):
            return "paused"
        if self._window(now) is None:
            return "quiet_hours"
        if self.repository.proactive_sent_since(user_id, character_id, self._day_start(now)):
            return "daily_limit"
        recent = self.repository.recent_completed_messages(user_id, character_id, 1)
        if not recent:
            return "no_conversation"
        last_user = datetime.fromisoformat(recent[0]["user_at"])
        if now.astimezone(timezone.utc) - last_user < timedelta(hours=6):
            return "recent_conversation"
        last_contact = self.repository.recent_proactive(user_id, character_id)
        if last_contact and datetime.fromisoformat(last_contact["created_at"]) >= last_user:
            return "awaiting_reply"
        return "allowed"

    def _messages(self, character, user_id, character_id, now, follow_up=None):
        system = (
            "你是虚拟角色，正在决定是否主动联系用户。当前时间：" + now.isoformat() +
            "。角色名称：" + character["display_name"] +
            "；性格：" + character["personality"] +
            "；表达习惯：" + character["speaking_style"] +
            "；互动边界：" + character["boundaries"] +
            "。用户已允许主动联系，但本次可以完全不联系。只根据给出的真实对话判断，"
            "不要编造经历、约定或你在现实中做过的事；不要用内疚或催促让用户回复。"
            "如没有自然且具体的理由，调用 skip_proactive；否则调用 propose_proactive。"
            "只调用一个工具，不直接输出文字。"
        )
        if follow_up:
            system += ("\n当前优先判断有来源的待跟进事项：" + follow_up["topic"] +
                       "。用户原话：" + follow_up["source_text"][:500] +
                       "。最早联系时间：" + follow_up["due_at"] +
                       "；过期时间：" + follow_up["expires_at"] +
                       "。如果近期对话表明事项已取消或结果已说过，调用 skip_proactive。")
            memories = MemoryService(
                self.settings, self.repository, self.provider, self.clock
            ).recall(user_id, character_id, follow_up["topic"], limit=2,
                     proactive=True)
            for memory in memories:
                system += "\n相关记忆（" + memory["occurred_at"] + "）：" + memory["text"]
        preferences = self.repository.list_preferences(user_id, character_id)
        if preferences:
            system += "\n用户当前明确规则：" + "；".join(
                row["kind"] + "=" + row["value"] for row in preferences
            )
        last_contact = self.repository.recent_proactive(user_id, character_id)
        if last_contact:
            system += "\n上次主动联系：" + last_contact["text"]
        rows = self.repository.recent_completed_messages(
            user_id, character_id, 5, for_context=True
        )
        messages = [{"role": "system", "content": system}]
        for row in reversed(rows):
            messages.append({"role": "user", "content": "[" + row["user_at"] + "] " + row["user_text"][:400]})
            messages.append({"role": "assistant", "content": row["assistant_text"][:400]})
        return messages

    @staticmethod
    def _parse_decision(assistant):
        calls = assistant.get("tool_calls") or []
        if len(calls) != 1:
            raise MiraError("invalid_proactive_decision")
        name = calls[0]["function"]["name"]
        try:
            arguments = json.loads(calls[0]["function"]["arguments"])
        except (ValueError, TypeError) as exc:
            raise MiraError("invalid_proactive_decision") from exc
        if not isinstance(arguments, dict):
            raise MiraError("invalid_proactive_decision")
        reason = arguments.get("reason")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 200:
            raise MiraError("invalid_proactive_decision")
        if name == "skip_proactive" and set(arguments) == {"reason"}:
            return "skip", None, reason.strip()
        text = arguments.get("text")
        if (name != "propose_proactive" or set(arguments) != {"text", "reason"} or
            not isinstance(text, str) or not text.strip() or len(text) > 200):
            raise MiraError("invalid_proactive_decision")
        return "send", text.strip(), reason.strip()
