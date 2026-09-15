"""JSON-safe memory contracts shared by context, tools, and commands."""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class MemoryDomainError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class MemoryInfrastructureError(Exception):
    def __init__(self, code: str = "database_error") -> None:
        self.code = code
        super().__init__(code)


class MemoryEntry(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID
    scope: Literal["group", "user"]
    user_id: UUID | None
    member_name: str | None = None
    category: str
    content: str
    normalized_key: str | None
    importance: int
    status: str
    source_message_id: UUID | None

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class MemberEntry(BaseModel):
    id: UUID
    display_name: str
    username: str | None


class SourceMessage(BaseModel):
    id: UUID
    telegram_message_id: int
    reply_to_telegram_message_id: int | None = None
    text: str
    is_bot_message: bool
    user_id: UUID | None


class MemoryMutation(BaseModel):
    action: Literal["saved", "updated", "deleted", "unchanged", "suppressed"]
    memory: MemoryEntry

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
