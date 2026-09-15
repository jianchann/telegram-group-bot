"""Rolling-summary data contracts."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


class SummaryConflict(Exception):
    """A concurrent invocation advanced the summary first."""


class SummaryInfrastructureError(Exception):
    """Summary storage is unavailable."""


@dataclass(frozen=True)
class SummaryState:
    id: UUID
    chat_id: UUID
    summary: str
    through_message_id: UUID | None
    through_telegram_message_id: int | None
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class SummaryMessage:
    id: UUID
    telegram_message_id: int
    speaker: str
    text: str
    is_bot_message: bool

    @property
    def prompt_text(self) -> str:
        return f"{self.speaker}: {self.text}"


@dataclass(frozen=True)
class SummaryBatch:
    state: SummaryState | None
    messages: list[SummaryMessage]
    unsummarized_count: int
    unsummarized_chars: int
    should_summarize: bool
