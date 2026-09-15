from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AIRun, Chat, ChatMember, Message, RateLimitEvent, UpdateProcessing, User
from app.db.session import Database


@dataclass(frozen=True)
class AcceptedMessage:
    chat_id: UUID
    user_id: UUID | None
    message_id: UUID
    telegram_message_id: int
    bot_enabled: bool = True


@dataclass(frozen=True)
class MessageRecord:
    telegram_message_id: int
    text: str | None
    display_name: str
    is_bot_message: bool
    created_at: datetime


def _display_name(sender: dict[str, Any]) -> str:
    return " ".join(str(sender.get(key, "")) for key in ("first_name", "last_name")).strip() or str(
        sender.get("title") or sender.get("username") or "Unknown speaker"
    )


class BotRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def _user(self, session: AsyncSession, sender: dict[str, Any]) -> UUID:
        statement = insert(User).values(
            telegram_user_id=sender["id"],
            username=sender.get("username"),
            display_name=_display_name(sender),
        )
        returning_statement = statement.on_conflict_do_update(
            index_elements=[User.telegram_user_id],
            set_={
                "username": statement.excluded.username,
                "display_name": statement.excluded.display_name,
                "updated_at": func.now(),
            },
        ).returning(User.id)
        return (await session.execute(returning_statement)).scalar_one()

    async def _message(
        self, session: AsyncSession, chat_id: UUID, payload: dict[str, Any], is_bot: bool
    ) -> tuple[UUID | None, UUID | None]:
        sender = payload.get("from") if not payload.get("sender_chat") else None
        user_id = await self._user(session, sender) if sender else None
        if user_id is not None:
            membership = insert(ChatMember).values(chat_id=chat_id, user_id=user_id)
            await session.execute(
                membership.on_conflict_do_update(
                    index_elements=[ChatMember.chat_id, ChatMember.user_id],
                    set_={"last_seen_at": func.now(), "is_active": True},
                )
            )
        message_type = next(
            (kind for kind in ("text", "photo", "document", "location", "poll") if kind in payload),
            "other",
        )
        message = insert(Message).values(
            chat_id=chat_id,
            user_id=user_id,
            telegram_message_id=payload["message_id"],
            reply_to_telegram_message_id=payload.get("reply_to_message", {}).get("message_id"),
            message_type=message_type,
            text=payload.get("text") or payload.get("caption"),
            raw_payload=payload,
            is_bot_message=is_bot,
            created_at=datetime.fromtimestamp(payload["date"], UTC),
        )
        result = await session.execute(
            message.on_conflict_do_nothing(
                index_elements=[Message.chat_id, Message.telegram_message_id]
            ).returning(Message.id)
        )
        return result.scalar_one_or_none(), user_id

    async def accept_message(
        self, update_id: int, message: dict[str, Any], default_timezone: str
    ) -> AcceptedMessage | None:
        async with self.database.sessions.begin() as session:
            claim = await session.execute(
                insert(UpdateProcessing)
                .values(update_id=update_id)
                .on_conflict_do_nothing()
                .returning(UpdateProcessing.update_id)
            )
            if claim.scalar_one_or_none() is None:
                return None
            payload = message["chat"]
            chat = insert(Chat).values(
                telegram_chat_id=payload["id"],
                title=payload.get("title"),
                chat_type=payload["type"],
                timezone=default_timezone,
            )
            returning_chat = chat.on_conflict_do_update(
                index_elements=[Chat.telegram_chat_id],
                set_={
                    "title": chat.excluded.title,
                    "chat_type": chat.excluded.chat_type,
                    "updated_at": func.now(),
                },
            ).returning(Chat.id, Chat.bot_enabled)
            chat_id, bot_enabled = (await session.execute(returning_chat)).one()
            message_id, user_id = await self._message(session, chat_id, message, False)
            if message_id is None:
                await session.execute(
                    update(UpdateProcessing)
                    .where(UpdateProcessing.update_id == update_id)
                    .values(status="duplicate_message", updated_at=func.now())
                )
                return None
            return AcceptedMessage(chat_id, user_id, message_id, message["message_id"], bot_enabled)

    async def recent_messages(
        self, chat_id: UUID, before_message_id: int, limit: int
    ) -> list[MessageRecord]:
        async with self.database.sessions() as session:
            result = await session.execute(
                select(Message, User.display_name)
                .outerjoin(User, Message.user_id == User.id)
                .where(Message.chat_id == chat_id, Message.telegram_message_id < before_message_id)
                .order_by(Message.telegram_message_id.desc())
                .limit(limit)
            )
            rows = list(result.all())
            return [
                MessageRecord(
                    message.telegram_message_id,
                    message.text,
                    name or _display_name((message.raw_payload or {}).get("sender_chat", {})),
                    message.is_bot_message,
                    message.created_at,
                )
                for message, name in reversed(rows)
            ]

    async def reserve_ai_request(
        self, chat_id: UUID, user_id: UUID, user_limit: int, chat_limit: int
    ) -> bool:
        async with self.database.sessions.begin() as session:
            await session.execute(select(Chat.id).where(Chat.id == chat_id).with_for_update())
            now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
            cutoff = now - timedelta(seconds=60)
            await session.execute(
                delete(RateLimitEvent).where(
                    RateLimitEvent.chat_id == chat_id, RateLimitEvent.created_at <= cutoff
                )
            )
            chat_count = (
                await session.execute(
                    select(func.count())
                    .select_from(RateLimitEvent)
                    .where(RateLimitEvent.chat_id == chat_id)
                )
            ).scalar_one()
            user_count = (
                await session.execute(
                    select(func.count())
                    .select_from(RateLimitEvent)
                    .where(RateLimitEvent.chat_id == chat_id, RateLimitEvent.user_id == user_id)
                )
            ).scalar_one()
            if chat_count >= chat_limit or user_count >= user_limit:
                return False
            session.add(RateLimitEvent(chat_id=chat_id, user_id=user_id, created_at=now))
            return True

    async def finish_update(
        self, update_id: int, status: str, error_code: str | None = None
    ) -> None:
        async with self.database.sessions.begin() as session:
            await session.execute(
                update(UpdateProcessing)
                .where(UpdateProcessing.update_id == update_id)
                .values(status=status, error_code=error_code, updated_at=func.now())
            )

    async def save_outgoing(self, chat_id: UUID, message: dict[str, Any]) -> None:
        async with self.database.sessions.begin() as session:
            await self._message(session, chat_id, message, True)

    async def telegram_chat_id(
        self, chat_id: UUID, actor_id: UUID, source_message_id: UUID
    ) -> int | None:
        async with self.database.sessions() as session:
            telegram_chat_id = (
                await session.execute(
                    select(Chat.telegram_chat_id)
                    .join(
                        ChatMember,
                        (ChatMember.chat_id == Chat.id)
                        & (ChatMember.user_id == actor_id)
                        & (ChatMember.is_active.is_(True)),
                    )
                    .where(Chat.id == chat_id, Chat.bot_enabled.is_(True))
                )
            ).scalar_one_or_none()
            if telegram_chat_id is None:
                return None
            source = (
                await session.execute(
                    select(Message.id).where(
                        Message.id == source_message_id,
                        Message.chat_id == chat_id,
                        Message.user_id == actor_id,
                        Message.is_bot_message.is_(False),
                    )
                )
            ).scalar_one_or_none()
            return telegram_chat_id if source is not None else None

    async def record_ai_run(
        self,
        chat_id: UUID,
        trigger_message_id: UUID,
        model: str,
        status: str,
        input_tokens: int | None,
        output_tokens: int | None,
        latency_ms: int,
        error_code: str | None = None,
        used_custom_functions: bool = False,
        used_search: bool = False,
        used_maps: bool = False,
        used_url_context: bool = False,
        used_code_execution: bool = False,
        request_type: str = "chat",
    ) -> None:
        async with self.database.sessions.begin() as session:
            session.add(
                AIRun(
                    chat_id=chat_id,
                    trigger_message_id=trigger_message_id,
                    model=model,
                    status=status,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=latency_ms,
                    error_code=error_code,
                    used_custom_functions=used_custom_functions,
                    used_search=used_search,
                    used_maps=used_maps,
                    used_url_context=used_url_context,
                    used_code_execution=used_code_execution,
                    request_type=request_type,
                )
            )
