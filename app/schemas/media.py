"""Invocation-local media passed to Gemini without durable byte storage."""

from dataclasses import dataclass


@dataclass(frozen=True)
class MediaAttachment:
    data: bytes
    mime_type: str
    source_telegram_message_id: int
    file_name: str | None = None


class MediaError(Exception):
    def __init__(self, code: str, user_message: str) -> None:
        self.code = code
        self.user_message = user_message
        super().__init__(code)
