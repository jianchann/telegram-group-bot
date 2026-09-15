"""Current-chat memory transactions; writes serialize on their owning chat."""

import re
import unicodedata
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import case, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import Chat, ChatMember, Memory, Message, User
from app.db.session import Database
from app.schemas.memory import (
    MemberEntry,
    MemoryDomainError,
    MemoryEntry,
    MemoryMutation,
    SourceMessage,
)


def normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def is_bot(message: Message) -> bool:
    return message.is_bot_message or bool(
        ((message.raw_payload or {}).get("from") or {}).get("is_bot")
    )


SEARCH_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "bot",
    "can",
    "could",
    "do",
    "does",
    "for",
    "from",
    "have",
    "how",
    "i",
    "in",
    "is",
    "it",
    "me",
    "memory",
    "memories",
    "my",
    "of",
    "on",
    "or",
    "our",
    "please",
    "remember",
    "that",
    "the",
    "their",
    "this",
    "to",
    "us",
    "was",
    "we",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "with",
    "would",
    "you",
    "your",
    "about",
    "tell",
    "know",
}


def search_terms(query: str) -> list[str]:
    # Keep SQL wildcard characters literal, even for a query containing only "%".
    words = re.findall(r"[^\W_]+(?:['’][^\W_]+)*|[%_\\]", normalize(query))
    return list(dict.fromkeys(word for word in words if word not in SEARCH_STOPWORDS))[:32]


class MemoryRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def _chat(self, session: AsyncSession, chat_id: UUID, lock: bool = False) -> Chat:
        statement = select(Chat).where(Chat.id == chat_id)
        if lock:
            statement = statement.with_for_update()
        chat = (await session.execute(statement)).scalar_one_or_none()
        if chat is None:
            raise MemoryDomainError("chat_not_found")
        return chat

    async def _member(self, session: AsyncSession, chat_id: UUID, user_id: UUID) -> User:
        user = (
            await session.execute(
                select(User)
                .join(ChatMember, ChatMember.user_id == User.id)
                .where(
                    ChatMember.chat_id == chat_id,
                    ChatMember.user_id == user_id,
                    ChatMember.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if user is None:
            raise MemoryDomainError("member_not_found")
        human_message = (
            await session.execute(
                select(Message)
                .where(
                    Message.chat_id == chat_id,
                    Message.user_id == user_id,
                    Message.is_bot_message.is_(False),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if human_message is None or is_bot(human_message):
            raise MemoryDomainError("member_not_found")
        return user

    async def _source(self, session: AsyncSession, chat_id: UUID, source_id: UUID) -> Message:
        source = (
            await session.execute(
                select(Message).where(Message.chat_id == chat_id, Message.id == source_id)
            )
        ).scalar_one_or_none()
        if source is None or is_bot(source):
            raise MemoryDomainError("source_not_found")
        return source

    async def _entry(self, session: AsyncSession, memory: Memory) -> MemoryEntry:
        name = None
        if memory.user_id is not None:
            name = (
                await session.execute(select(User.display_name).where(User.id == memory.user_id))
            ).scalar_one_or_none()
        return MemoryEntry(
            id=memory.id,
            scope=cast(Literal["group", "user"], memory.scope),
            user_id=memory.user_id,
            member_name=name,
            category=memory.category,
            content=memory.content,
            normalized_key=memory.normalized_key,
            importance=memory.importance,
            status=memory.status,
            source_message_id=memory.source_message_id,
        )

    async def members(self, chat_id: UUID) -> list[MemberEntry]:
        async with self.database.sessions() as session:
            await self._chat(session, chat_id)
            rows = (
                (
                    await session.execute(
                        select(User)
                        .join(ChatMember, ChatMember.user_id == User.id)
                        .where(ChatMember.chat_id == chat_id, ChatMember.is_active.is_(True))
                        .order_by(User.display_name, User.id)
                    )
                )
                .scalars()
                .all()
            )
            result = []
            for user in rows:
                try:
                    await self._member(session, chat_id, user.id)
                except MemoryDomainError:
                    continue
                result.append(
                    MemberEntry(
                        id=user.id,
                        display_name=user.display_name or "Unknown speaker",
                        username=user.username,
                    )
                )
            return result

    async def sources(self, chat_id: UUID, telegram_message_ids: list[int]) -> list[SourceMessage]:
        async with self.database.sessions() as session:
            await self._chat(session, chat_id)
            messages = (
                (
                    await session.execute(
                        select(Message)
                        .where(
                            Message.chat_id == chat_id,
                            Message.telegram_message_id.in_(telegram_message_ids),
                        )
                        .order_by(Message.telegram_message_id)
                    )
                )
                .scalars()
                .all()
            )
            if {message.telegram_message_id for message in messages} != set(telegram_message_ids):
                raise MemoryDomainError("source_not_found")
            return [
                SourceMessage(
                    id=message.id,
                    telegram_message_id=message.telegram_message_id,
                    reply_to_telegram_message_id=message.reply_to_telegram_message_id,
                    text=message.text or "",
                    is_bot_message=is_bot(message),
                    user_id=message.user_id,
                )
                for message in messages
            ]

    async def settings(self, chat_id: UUID) -> dict[str, Any]:
        async with self.database.sessions() as session:
            return dict((await self._chat(session, chat_id)).settings)

    async def search(
        self,
        chat_id: UUID,
        query: str = "",
        scope: str = "all",
        user_id: UUID | None = None,
        limit: int = 20,
    ) -> list[MemoryEntry]:
        async with self.database.sessions() as session:
            await self._chat(session, chat_id)
            if user_id is not None:
                await self._member(session, chat_id, user_id)
            statement = select(Memory).where(Memory.chat_id == chat_id, Memory.status == "active")
            if scope != "all":
                statement = statement.where(Memory.scope == scope)
            if user_id is not None:
                statement = statement.where(Memory.user_id == user_id)
            conditions: list[ColumnElement[bool]] = []
            match_score: ColumnElement[int] = literal(0)
            for term in search_terms(query):
                pattern = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                condition = or_(
                    Memory.normalized_content.ilike(f"%{pattern}%", escape="\\"),
                    Memory.normalized_key.ilike(f"%{pattern}%", escape="\\"),
                )
                conditions.append(condition)
                match_score = match_score + case((condition, 1), else_=0)
            if conditions:
                statement = statement.where(or_(*conditions))
            rows = (
                (
                    await session.execute(
                        statement.order_by(
                            match_score.desc(),
                            Memory.importance.desc(),
                            Memory.updated_at.desc(),
                            Memory.id,
                        ).limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            return [await self._entry(session, row) for row in rows]

    async def list_page(
        self, chat_id: UUID, user_id: UUID | None = None, page: int = 1, page_size: int = 20
    ) -> tuple[list[MemoryEntry], int]:
        async with self.database.sessions() as session:
            await self._chat(session, chat_id)
            if user_id is not None:
                await self._member(session, chat_id, user_id)
            predicate = [Memory.chat_id == chat_id, Memory.status == "active"]
            if user_id is not None:
                predicate.append(Memory.user_id == user_id)
            total = (
                await session.execute(select(func.count()).select_from(Memory).where(*predicate))
            ).scalar_one()
            rows = (
                (
                    await session.execute(
                        select(Memory)
                        .where(*predicate)
                        .order_by(Memory.scope, Memory.created_at, Memory.id)
                        .offset((page - 1) * page_size)
                        .limit(page_size)
                    )
                )
                .scalars()
                .all()
            )
            return [await self._entry(session, row) for row in rows], total

    async def _validate_write(
        self,
        session: AsyncSession,
        chat_id: UUID,
        actor_id: UUID,
        source_id: UUID,
        user_id: UUID | None = None,
    ) -> None:
        await self._chat(session, chat_id, lock=True)
        await self._member(session, chat_id, actor_id)
        await self._source(session, chat_id, source_id)
        if user_id is not None:
            await self._member(session, chat_id, user_id)

    async def save(
        self,
        chat_id: UUID,
        actor_id: UUID,
        source_id: UUID,
        scope: Literal["group", "user"],
        content: str,
        category: str = "other",
        user_id: UUID | None = None,
        normalized_key: str | None = None,
        importance: int = 5,
        explicit: bool = False,
    ) -> MemoryMutation:
        async with self.database.sessions.begin() as session:
            await self._validate_write(session, chat_id, actor_id, source_id, user_id)
            normalized_content = normalize(content)
            key = normalize(normalized_key) if normalized_key else None
            related = (
                (
                    await session.execute(
                        select(Memory)
                        .where(
                            Memory.chat_id == chat_id,
                            Memory.scope == scope,
                            Memory.user_id == user_id,
                        )
                        .order_by(Memory.created_at.desc(), Memory.id)
                    )
                )
                .scalars()
                .all()
            )
            for memory in related:
                if memory.status == "active" and memory.normalized_content == normalized_content:
                    return MemoryMutation(
                        action="unchanged", memory=await self._entry(session, memory)
                    )
            if not explicit:
                for memory in related:
                    if memory.status == "deleted" and (
                        memory.normalized_content == normalized_content
                        or (key is not None and memory.normalized_key == key)
                    ):
                        return MemoryMutation(
                            action="suppressed", memory=await self._entry(session, memory)
                        )
            now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
            if key is not None:
                for memory in related:
                    if memory.status == "active" and memory.normalized_key == key:
                        memory.status = "superseded"
                        memory.updated_at = now
                        memory.latest_actor_user_id = actor_id
                        memory.latest_source_message_id = source_id
            memory = Memory(
                chat_id=chat_id,
                scope=scope,
                user_id=user_id,
                category=category,
                content=content,
                normalized_content=normalized_content,
                normalized_key=key,
                importance=importance,
                status="active",
                source_message_id=source_id,
                source_kind="explicit" if explicit else "conversation",
                created_by_user_id=actor_id,
                created_by_model=not explicit,
                latest_actor_user_id=actor_id,
                latest_source_message_id=source_id,
                updated_at=now,
            )
            session.add(memory)
            await session.flush()
            return MemoryMutation(action="saved", memory=await self._entry(session, memory))

    async def _target(self, session: AsyncSession, chat_id: UUID, memory_id: UUID) -> Memory:
        memory = (
            await session.execute(
                select(Memory).where(Memory.chat_id == chat_id, Memory.id == memory_id)
            )
        ).scalar_one_or_none()
        if memory is None:
            raise MemoryDomainError("memory_not_found")
        if memory.user_id is not None:
            await self._member(session, chat_id, memory.user_id)
        return memory

    async def update(
        self,
        chat_id: UUID,
        actor_id: UUID,
        source_id: UUID,
        memory_id: UUID,
        content: str | None = None,
        category: str | None = None,
        importance: int | None = None,
    ) -> MemoryMutation:
        async with self.database.sessions.begin() as session:
            await self._validate_write(session, chat_id, actor_id, source_id)
            memory = await self._target(session, chat_id, memory_id)
            if memory.status != "active":
                raise MemoryDomainError("memory_not_active")
            if content is not None and normalize(content) != memory.normalized_content:
                duplicate = (
                    await session.execute(
                        select(Memory.id).where(
                            Memory.chat_id == chat_id,
                            Memory.scope == memory.scope,
                            Memory.user_id == memory.user_id,
                            Memory.status == "active",
                            Memory.normalized_content == normalize(content),
                            Memory.id != memory.id,
                        )
                    )
                ).scalar_one_or_none()
                if duplicate is not None:
                    raise MemoryDomainError("duplicate_memory")
            changed = any(
                value is not None and value != getattr(memory, field)
                for field, value in (
                    ("content", content),
                    ("category", category),
                    ("importance", importance),
                )
            )
            if not changed:
                return MemoryMutation(action="unchanged", memory=await self._entry(session, memory))
            if content is not None:
                memory.content = content
                memory.normalized_content = normalize(content)
            if category is not None:
                memory.category = category
            if importance is not None:
                memory.importance = importance
            memory.latest_actor_user_id = actor_id
            memory.latest_source_message_id = source_id
            memory.updated_at = (await session.execute(select(func.clock_timestamp()))).scalar_one()
            await session.flush()
            return MemoryMutation(action="updated", memory=await self._entry(session, memory))

    async def delete(
        self, chat_id: UUID, actor_id: UUID, source_id: UUID, memory_id: UUID
    ) -> MemoryMutation:
        async with self.database.sessions.begin() as session:
            await self._validate_write(session, chat_id, actor_id, source_id)
            memory = await self._target(session, chat_id, memory_id)
            if memory.status == "deleted":
                return MemoryMutation(action="unchanged", memory=await self._entry(session, memory))
            if memory.status != "active":
                raise MemoryDomainError("memory_not_active")
            memory.status = "deleted"
            memory.latest_actor_user_id = actor_id
            memory.latest_source_message_id = source_id
            memory.updated_at = (await session.execute(select(func.clock_timestamp()))).scalar_one()
            await session.flush()
            return MemoryMutation(action="deleted", memory=await self._entry(session, memory))
