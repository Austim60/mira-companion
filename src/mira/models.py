from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class MessageInput:
    user_id: str
    character_id: str
    source: str
    source_message_id: str
    text: str
    occurred_at: str


@dataclass(frozen=True)
class ConversationResult:
    turn_id: str
    status: str
    text: Optional[str] = None
    error_code: Optional[str] = None
    retryable: bool = False
    quote_current_message: bool = False


class MiraError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)
