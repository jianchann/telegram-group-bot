"""JSON-safe plan service contracts."""

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PlanDomainError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class PlanInfrastructureError(Exception):
    def __init__(self, code: str = "database_error"):
        self.code = code
        super().__init__(code)


class JsonModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class PlanMemberEntry(JsonModel):
    user_id: UUID
    display_name: str
    role: str | None
    status: str


class PlanItemEntry(JsonModel):
    id: UUID
    plan_id: UUID
    item_type: str
    title: str
    description: str | None
    status: str
    assigned_user_id: UUID | None
    assigned_name: str | None
    due_at: datetime | None
    category: str | None
    position: int | None
    metadata: dict[str, Any]
    source_message_id: UUID | None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class TaskEntry(JsonModel):
    id: UUID
    plan_id: UUID
    plan_name: str
    title: str
    status: Literal["open", "in_progress"]
    assigned_user_id: UUID | None
    assigned_name: str | None
    due_at: datetime | None


class PlanArtifactEntry(JsonModel):
    id: UUID
    plan_id: UUID
    artifact_type: Literal["url"] = "url"
    title: str | None
    url: str
    normalized_url: str
    category: str | None
    metadata: dict[str, Any]
    source_message_id: UUID | None
    created_by_user_id: UUID | None
    latest_actor_user_id: UUID | None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PlanEntry(JsonModel):
    id: UUID
    chat_id: UUID
    name: str
    description: str | None
    plan_type: str | None
    status: str
    start_date: date | None
    end_date: date | None
    timezone: str | None
    metadata: dict[str, Any]
    created_by_user_id: UUID | None
    members: list[PlanMemberEntry] = Field(default_factory=list)
    items: list[PlanItemEntry] = Field(default_factory=list)
    artifacts: list[PlanArtifactEntry] = Field(default_factory=list)
    total_item_count: int = 0
    total_artifact_count: int = 0


class PlanMutation(JsonModel):
    action: Literal["created", "updated", "deleted", "unchanged"]
    plan: PlanEntry | None = None
    item: PlanItemEntry | None = None
    member: PlanMemberEntry | None = None
    artifact: PlanArtifactEntry | None = None


class PlanResolution(JsonModel):
    plan: PlanEntry | None
    candidates: list[PlanEntry]
    reason: Literal["explicit", "reply", "recent", "sole_active", "ambiguous", "none"]
