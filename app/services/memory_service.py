"""Validated memory API; semantic eligibility belongs to the tool policy layer."""

from collections.abc import Awaitable
from typing import Any, Literal, TypeVar, cast
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from app.db.session import Database
from app.repositories.memories import MemoryRepository
from app.schemas.memory import (
    MemberEntry,
    MemoryDomainError,
    MemoryEntry,
    MemoryInfrastructureError,
    MemoryMutation,
    SourceMessage,
)

T = TypeVar("T")
CATEGORIES = {
    "preference",
    "constraint",
    "profile",
    "decision",
    "travel",
    "food",
    "budget",
    "logistics",
    "other",
}


class MemoryService:
    def __init__(self, database: Database) -> None:
        self.repository = MemoryRepository(database)

    async def _run(self, operation: Awaitable[T]) -> T:
        try:
            return await operation
        except SQLAlchemyError:
            raise MemoryInfrastructureError() from None

    def _content(self, content: str) -> str:
        content = content.strip()
        if not content or len(content) > 2000:
            raise MemoryDomainError("invalid_content")
        return content

    def _category(self, category: str) -> str:
        if category not in CATEGORIES:
            raise MemoryDomainError("invalid_category")
        return category

    def _importance(self, importance: int) -> int:
        if isinstance(importance, bool) or not 1 <= importance <= 10:
            raise MemoryDomainError("invalid_importance")
        return importance

    async def members(self, chat_id: UUID) -> list[MemberEntry]:
        return await self._run(self.repository.members(chat_id))

    async def sources(self, chat_id: UUID, telegram_message_ids: list[int]) -> list[SourceMessage]:
        return await self._run(self.repository.sources(chat_id, telegram_message_ids))

    async def settings(self, chat_id: UUID) -> dict[str, Any]:
        settings = await self._run(self.repository.settings(chat_id))
        return {
            **settings,
            "auto_memory_enabled": settings.get("auto_memory_enabled", True) is True,
        }

    async def search(
        self,
        chat_id: UUID,
        query: str = "",
        scope: str = "all",
        user_id: UUID | None = None,
        limit: int = 20,
    ) -> list[MemoryEntry]:
        if scope not in {"all", "group", "user"}:
            raise MemoryDomainError("invalid_scope")
        if scope == "group" and user_id is not None:
            raise MemoryDomainError("invalid_scope")
        if not 1 <= limit <= 100 or len(query) > 2000:
            raise MemoryDomainError("invalid_query")
        return await self._run(self.repository.search(chat_id, query, scope, user_id, limit))

    async def list_page(
        self, chat_id: UUID, user_id: UUID | None = None, page: int = 1, page_size: int = 20
    ) -> tuple[list[MemoryEntry], int]:
        if page < 1 or not 1 <= page_size <= 100:
            raise MemoryDomainError("invalid_page")
        return await self._run(self.repository.list_page(chat_id, user_id, page, page_size))

    async def save(
        self,
        chat_id: UUID,
        actor_id: UUID,
        source_id: UUID,
        scope: str,
        content: str,
        category: str = "other",
        user_id: UUID | None = None,
        normalized_key: str | None = None,
        importance: int = 5,
        explicit: bool = False,
    ) -> MemoryMutation:
        if (
            scope not in {"group", "user"}
            or (scope == "group" and user_id is not None)
            or (scope == "user" and user_id is None)
        ):
            raise MemoryDomainError("invalid_scope")
        if normalized_key is not None:
            normalized_key = normalized_key.strip() or None
            if normalized_key is not None and len(normalized_key) > 200:
                raise MemoryDomainError("invalid_key")
        return await self._run(
            self.repository.save(
                chat_id,
                actor_id,
                source_id,
                cast(Literal["group", "user"], scope),
                self._content(content),
                self._category(category),
                user_id,
                normalized_key,
                self._importance(importance),
                explicit,
            )
        )

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
        return await self._run(
            self.repository.update(
                chat_id,
                actor_id,
                source_id,
                memory_id,
                self._content(content) if content is not None else None,
                self._category(category) if category is not None else None,
                self._importance(importance) if importance is not None else None,
            )
        )

    async def delete(
        self, chat_id: UUID, actor_id: UUID, source_id: UUID, memory_id: UUID
    ) -> MemoryMutation:
        return await self._run(self.repository.delete(chat_id, actor_id, source_id, memory_id))
