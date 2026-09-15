"""Phase 1 durable state; Telegram identifiers are never internal identities."""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Chat(Base):
    __tablename__ = "chats"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    title: Mapped[str | None] = mapped_column(Text)
    chat_type: Mapped[str] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(Text, default="Asia/Manila")
    bot_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    username: Mapped[str | None] = mapped_column(Text)
    display_name: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChatMember(Base):
    __tablename__ = "chat_members"
    chat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chats.id"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    display_name_override: Mapped[str | None] = mapped_column(Text)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("chat_id", "telegram_message_id", name="uq_messages_chat_telegram"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    chat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chats.id"), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    telegram_message_id: Mapped[int] = mapped_column(BigInteger)
    reply_to_telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    message_type: Mapped[str] = mapped_column(Text)
    text: Mapped[str | None] = mapped_column(Text)
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    is_bot_message: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ChatSummary(Base):
    __tablename__ = "chat_summaries"
    __table_args__ = (UniqueConstraint("chat_id", name="uq_chat_summaries_chat_id"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    chat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chats.id"))
    summary: Mapped[str] = mapped_column(Text)
    through_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"))
    summary_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UpdateProcessing(Base):
    __tablename__ = "update_processing"
    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    status: Mapped[str] = mapped_column(Text, default="claimed")
    error_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RateLimitEvent(Base):
    __tablename__ = "rate_limit_events"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    chat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chats.id"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, server_default=func.now()
    )


class AIRun(Base):
    __tablename__ = "ai_runs"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    chat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chats.id"))
    trigger_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"))
    provider: Mapped[str] = mapped_column(Text, default="google")
    model: Mapped[str] = mapped_column(Text)
    request_type: Mapped[str] = mapped_column(Text, default="chat")
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    used_search: Mapped[bool] = mapped_column(Boolean, default=False)
    used_maps: Mapped[bool] = mapped_column(Boolean, default=False)
    used_url_context: Mapped[bool] = mapped_column(Boolean, default=False)
    used_code_execution: Mapped[bool] = mapped_column(Boolean, default=False)
    used_custom_functions: Mapped[bool] = mapped_column(Boolean, default=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6))
    status: Mapped[str] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Memory(Base):
    __tablename__ = "memories"
    __table_args__ = (
        CheckConstraint(
            "(scope = 'group' AND user_id IS NULL) OR (scope = 'user' AND user_id IS NOT NULL)",
            name="ck_memories_scope",
        ),
        CheckConstraint("importance BETWEEN 1 AND 10", name="ck_memories_importance"),
        CheckConstraint("status IN ('active', 'superseded', 'deleted')", name="ck_memories_status"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    chat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chats.id"), index=True)
    scope: Mapped[str] = mapped_column(Text)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    category: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    normalized_content: Mapped[str] = mapped_column(Text)
    normalized_key: Mapped[str | None] = mapped_column(Text)
    importance: Mapped[int] = mapped_column(SmallInteger, default=5)
    status: Mapped[str] = mapped_column(Text, default="active")
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"))
    source_kind: Mapped[str] = mapped_column(Text, default="conversation")
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_by_model: Mapped[bool] = mapped_column(Boolean, default=False)
    latest_actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    latest_source_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Plan(Base):
    __tablename__ = "plans"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'active', 'completed', 'cancelled', 'archived')",
            name="ck_plans_status",
        ),
        CheckConstraint(
            "start_date IS NULL OR end_date IS NULL OR start_date <= end_date",
            name="ck_plans_dates",
        ),
        Index("ix_plans_chat_status_updated", "chat_id", "status", "updated_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    chat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chats.id"), index=True)
    name: Mapped[str] = mapped_column(Text)
    normalized_name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    plan_type: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="active")
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    timezone: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    latest_actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    latest_source_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PlanMember(Base):
    __tablename__ = "plan_members"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'removed')", name="ck_plan_members_status"),
        Index("ix_plan_members_user_id", "user_id"),
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plans.id"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    role: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="active")
    latest_actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    latest_source_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PlanItem(Base):
    __tablename__ = "plan_items"
    __table_args__ = (
        CheckConstraint(
            "item_type IN ('task', 'decision', 'constraint', 'activity', 'open_question', 'note')",
            name="ck_plan_items_type",
        ),
        CheckConstraint(
            "(item_type = 'task' AND status IN ('open','in_progress','done','cancelled')) OR (item_type = 'decision' AND status IN ('proposed','confirmed','reversed')) OR (item_type = 'constraint' AND status IN ('active','removed')) OR (item_type = 'activity' AND status IN ('idea','shortlisted','confirmed','completed','cancelled')) OR (item_type = 'open_question' AND status IN ('open','resolved')) OR (item_type = 'note' AND status IN ('active','removed'))",  # noqa: E501
            name="ck_plan_items_status",  # noqa: E501
        ),
        Index(
            "ix_plan_items_plan_type_status_position", "plan_id", "item_type", "status", "position"
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plans.id"), index=True)
    item_type: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    normalized_title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    category: Mapped[str | None] = mapped_column(Text)
    position: Mapped[int | None] = mapped_column(Integer)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_by_model: Mapped[bool] = mapped_column(Boolean, default=False)
    latest_actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    latest_source_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PlanArtifact(Base):
    __tablename__ = "plan_artifacts"
    __table_args__ = (
        CheckConstraint("artifact_type = 'url'", name="ck_plan_artifacts_type"),
        UniqueConstraint("plan_id", "normalized_url", name="uq_plan_artifacts_plan_url"),
        Index("ix_plan_artifacts_plan_category_created", "plan_id", "category", "created_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plans.id"), index=True)
    artifact_type: Mapped[str] = mapped_column(Text, default="url")
    title: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    normalized_url: Mapped[str] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    latest_actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    latest_source_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
