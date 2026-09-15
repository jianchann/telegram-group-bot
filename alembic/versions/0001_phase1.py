"""Initial core chat persistence and processing claims."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "0001_phase1"
down_revision = None
branch_labels = None
depends_on = None


def identity():
    return sa.Column("id", pg.UUID(as_uuid=True), primary_key=True)


def timestamp(name, default=True):
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now() if default else None,
    )


def upgrade():
    op.create_table(
        "chats",
        identity(),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("title", sa.Text()),
        sa.Column("chat_type", sa.Text(), nullable=False),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column("bot_enabled", sa.Boolean(), nullable=False),
        sa.Column("settings", pg.JSONB(), nullable=False),
        timestamp("created_at"),
        timestamp("updated_at"),
    )
    op.create_table(
        "users",
        identity(),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("username", sa.Text()),
        sa.Column("display_name", sa.Text()),
        timestamp("created_at"),
        timestamp("updated_at"),
    )
    op.create_table(
        "chat_members",
        sa.Column("chat_id", pg.UUID(), sa.ForeignKey("chats.id"), primary_key=True),
        sa.Column("user_id", pg.UUID(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("display_name_override", sa.Text()),
        sa.Column("is_admin", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        timestamp("joined_at"),
        timestamp("last_seen_at"),
    )
    op.create_table(
        "messages",
        identity(),
        sa.Column("chat_id", pg.UUID(), sa.ForeignKey("chats.id"), nullable=False),
        sa.Column("user_id", pg.UUID(), sa.ForeignKey("users.id")),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=False),
        sa.Column("reply_to_telegram_message_id", sa.BigInteger()),
        sa.Column("message_type", sa.Text(), nullable=False),
        sa.Column("text", sa.Text()),
        sa.Column("raw_payload", pg.JSONB()),
        sa.Column("is_bot_message", sa.Boolean(), nullable=False),
        timestamp("created_at", False),
        sa.UniqueConstraint("chat_id", "telegram_message_id", name="uq_messages_chat_telegram"),
    )
    op.create_index("ix_messages_chat_id", "messages", ["chat_id"])
    op.create_table(
        "update_processing",
        sa.Column("update_id", sa.BigInteger(), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text()),
        timestamp("created_at"),
        timestamp("updated_at"),
    )
    op.create_table(
        "rate_limit_events",
        identity(),
        sa.Column("chat_id", pg.UUID(), sa.ForeignKey("chats.id"), nullable=False),
        sa.Column("user_id", pg.UUID(), sa.ForeignKey("users.id"), nullable=False),
        timestamp("created_at"),
    )
    op.create_index("ix_rate_limit_events_chat_id", "rate_limit_events", ["chat_id"])
    op.create_index("ix_rate_limit_events_created_at", "rate_limit_events", ["created_at"])
    op.create_table(
        "ai_runs",
        identity(),
        sa.Column("chat_id", pg.UUID(), sa.ForeignKey("chats.id"), nullable=False),
        sa.Column("trigger_message_id", pg.UUID(), sa.ForeignKey("messages.id")),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("request_type", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        *(
            sa.Column(name, sa.Boolean(), nullable=False)
            for name in [
                "used_search",
                "used_maps",
                "used_url_context",
                "used_code_execution",
                "used_custom_functions",
            ]
        ),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("estimated_cost_usd", sa.Numeric(12, 6)),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text()),
        timestamp("created_at"),
    )


def downgrade():
    for name in (
        "ai_runs",
        "rate_limit_events",
        "update_processing",
        "messages",
        "chat_members",
        "users",
        "chats",
    ):
        op.drop_table(name)
