"""Durable group and personal memory, provenance, and deletion tombstones."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "0002_memory"
down_revision = "0001_phase1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "memories",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("chat_id", pg.UUID(), sa.ForeignKey("chats.id"), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("user_id", pg.UUID(), sa.ForeignKey("users.id")),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("normalized_content", sa.Text(), nullable=False),
        sa.Column("normalized_key", sa.Text()),
        sa.Column("importance", sa.SmallInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("source_message_id", pg.UUID(), sa.ForeignKey("messages.id")),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("created_by_user_id", pg.UUID(), sa.ForeignKey("users.id")),
        sa.Column("created_by_model", sa.Boolean(), nullable=False),
        sa.Column("latest_actor_user_id", pg.UUID(), sa.ForeignKey("users.id")),
        sa.Column("latest_source_message_id", pg.UUID(), sa.ForeignKey("messages.id")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "(scope = 'group' AND user_id IS NULL) OR (scope = 'user' AND user_id IS NOT NULL)",
            name="ck_memories_scope",
        ),
        sa.CheckConstraint("importance BETWEEN 1 AND 10", name="ck_memories_importance"),
        sa.CheckConstraint(
            "status IN ('active', 'superseded', 'deleted')", name="ck_memories_status"
        ),
    )
    op.create_index("ix_memories_chat_id", "memories", ["chat_id"])


def downgrade():
    op.drop_table("memories")
