"""Policy for invocation-triggered rolling summaries."""

from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from app.repositories.summaries import SummaryRepository
from app.schemas.summary import (
    SummaryBatch,
    SummaryConflict,
    SummaryInfrastructureError,
    SummaryState,
)


class SummaryService:
    def __init__(
        self,
        repository: SummaryRepository,
        *,
        message_threshold: int = 100,
        char_threshold: int = 40_000,
        preserve_recent: int = 30,
        max_input_chars: int = 20_000,
    ) -> None:
        self.repository = repository
        self.message_threshold = message_threshold
        self.char_threshold = char_threshold
        self.preserve_recent = preserve_recent
        self.max_input_chars = max_input_chars

    async def current(self, chat_id: UUID) -> SummaryState | None:
        try:
            return await self.repository.get(chat_id)
        except SQLAlchemyError:
            raise SummaryInfrastructureError() from None

    async def prepare(self, chat_id: UUID, before_telegram_message_id: int) -> SummaryBatch:
        try:
            state = await self.repository.get(chat_id)
            count, chars = await self.repository.unsummarized_stats(
                chat_id, before_telegram_message_id
            )
            should_summarize = count > self.message_threshold or chars > self.char_threshold
            messages = []
            if should_summarize:
                previous_chars = len(state.summary) + 1 if state is not None else 0
                messages = await self.repository.batch(
                    chat_id,
                    before_telegram_message_id,
                    self.preserve_recent,
                    max(0, self.max_input_chars - previous_chars),
                )
                should_summarize = bool(messages)
            return SummaryBatch(state, messages, count, chars, should_summarize)
        except SQLAlchemyError:
            raise SummaryInfrastructureError() from None

    async def save(
        self,
        chat_id: UUID,
        expected_version: int | None,
        summary: str,
        through_message_id: UUID,
    ) -> SummaryState:
        summary = summary.strip()
        if not summary:
            raise ValueError("summary must not be empty")
        try:
            result = await self.repository.compare_and_swap(
                chat_id, expected_version, summary, through_message_id
            )
        except SQLAlchemyError:
            raise SummaryInfrastructureError() from None
        if result is None:
            raise SummaryConflict()
        return result
