"""Chat-scoped rolling-summary persistence."""

from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from app.db.models import ChatSummary, Message, User
from app.db.session import Database
from app.repositories.bot import _display_name
from app.schemas.summary import SummaryMessage, SummaryState


def _state(row: ChatSummary, through_telegram_message_id: int | None = None) -> SummaryState:
    return SummaryState(
        id=row.id,
        chat_id=row.chat_id,
        summary=row.summary,
        through_message_id=row.through_message_id,
        through_telegram_message_id=through_telegram_message_id,
        version=row.summary_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SummaryRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def get(self, chat_id: UUID) -> SummaryState | None:
        async with self.database.sessions() as session:
            result = (
                await session.execute(
                    select(ChatSummary, Message.telegram_message_id)
                    .outerjoin(Message, ChatSummary.through_message_id == Message.id)
                    .where(ChatSummary.chat_id == chat_id)
                )
            ).one_or_none()
            return _state(result[0], result[1]) if result is not None else None

    async def unsummarized_stats(
        self, chat_id: UUID, before_telegram_message_id: int
    ) -> tuple[int, int]:
        async with self.database.sessions() as session:
            checkpoint = (
                await session.execute(
                    select(Message.telegram_message_id)
                    .join(ChatSummary, ChatSummary.through_message_id == Message.id)
                    .where(ChatSummary.chat_id == chat_id)
                )
            ).scalar_one_or_none()
            conditions: list[Any] = [
                Message.chat_id == chat_id,
                Message.telegram_message_id < before_telegram_message_id,
                Message.text.is_not(None),
                func.length(func.trim(Message.text)) > 0,
            ]
            if checkpoint is not None:
                conditions.append(Message.telegram_message_id > checkpoint)
            count, chars = (
                await session.execute(
                    select(
                        func.count(), func.coalesce(func.sum(func.length(Message.text)), 0)
                    ).where(*conditions)
                )
            ).one()
            return int(count), int(chars)

    async def batch(
        self,
        chat_id: UUID,
        before_telegram_message_id: int,
        preserve_recent: int,
        max_chars: int,
    ) -> list[SummaryMessage]:
        if max_chars <= 0:
            return []
        async with self.database.sessions() as session:
            checkpoint = (
                await session.execute(
                    select(Message.telegram_message_id)
                    .join(ChatSummary, ChatSummary.through_message_id == Message.id)
                    .where(ChatSummary.chat_id == chat_id)
                )
            ).scalar_one_or_none()
            conditions: list[Any] = [
                Message.chat_id == chat_id,
                Message.telegram_message_id < before_telegram_message_id,
                Message.text.is_not(None),
                func.length(func.trim(Message.text)) > 0,
            ]
            if checkpoint is not None:
                conditions.append(Message.telegram_message_id > checkpoint)
            cutoff = (
                await session.execute(
                    select(Message.telegram_message_id)
                    .where(*conditions)
                    .order_by(Message.telegram_message_id.desc())
                    .offset(preserve_recent)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if cutoff is None:
                return []
            rows = list(
                (
                    await session.execute(
                        select(Message, User.display_name)
                        .outerjoin(User, Message.user_id == User.id)
                        .where(*conditions, Message.telegram_message_id <= cutoff)
                        .order_by(Message.telegram_message_id)
                        .limit(1000)
                    )
                ).all()
            )
            result: list[SummaryMessage] = []
            used = 0
            for message, display_name in rows:
                speaker = display_name or _display_name(
                    (message.raw_payload or {}).get("sender_chat", {})
                )
                available = max_chars - used - len(speaker) - 3
                if available <= 0:
                    break
                text = message.text[:available]
                result.append(
                    SummaryMessage(
                        message.id,
                        message.telegram_message_id,
                        speaker[:200],
                        text,
                        message.is_bot_message,
                    )
                )
                used += len(result[-1].prompt_text) + 1
                if len(text) < len(message.text):
                    break
            return result

    async def compare_and_swap(
        self,
        chat_id: UUID,
        expected_version: int | None,
        summary: str,
        through_message_id: UUID,
    ) -> SummaryState | None:
        async with self.database.sessions.begin() as session:
            source_exists = (
                await session.execute(
                    select(Message.id).where(
                        Message.id == through_message_id, Message.chat_id == chat_id
                    )
                )
            ).scalar_one_or_none()
            if source_exists is None:
                return None
            if expected_version is None:
                insert_statement = (
                    insert(ChatSummary)
                    .values(
                        chat_id=chat_id,
                        summary=summary,
                        through_message_id=through_message_id,
                        summary_version=1,
                    )
                    .on_conflict_do_nothing(index_elements=[ChatSummary.chat_id])
                    .returning(ChatSummary)
                )
                row = (await session.execute(insert_statement)).scalar_one_or_none()
            else:
                update_statement = (
                    update(ChatSummary)
                    .where(
                        ChatSummary.chat_id == chat_id,
                        ChatSummary.summary_version == expected_version,
                    )
                    .values(
                        summary=summary,
                        through_message_id=through_message_id,
                        summary_version=expected_version + 1,
                        updated_at=func.now(),
                    )
                    .returning(ChatSummary)
                )
                row = (await session.execute(update_statement)).scalar_one_or_none()
            if row is None:
                return None
            telegram_id = (
                await session.execute(
                    select(Message.telegram_message_id).where(Message.id == through_message_id)
                )
            ).scalar_one()
            return _state(row, telegram_id)
