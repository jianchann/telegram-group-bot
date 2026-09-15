"""Structured plans, participants, and items."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "0003_plans"
down_revision = "0002_memory"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "plans",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("chat_id", pg.UUID(), sa.ForeignKey("chats.id"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("normalized_name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("plan_type", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("start_date", sa.Date()),
        sa.Column("end_date", sa.Date()),
        sa.Column("timezone", sa.Text()),
        sa.Column("metadata", pg.JSONB(), nullable=False),
        sa.Column("created_by_user_id", pg.UUID(), sa.ForeignKey("users.id")),
        sa.Column("latest_actor_user_id", pg.UUID(), sa.ForeignKey("users.id")),
        sa.Column("latest_source_message_id", pg.UUID(), sa.ForeignKey("messages.id")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'completed', 'cancelled', 'archived')",
            name="ck_plans_status",
        ),
        sa.CheckConstraint(
            "start_date IS NULL OR end_date IS NULL OR start_date <= end_date",
            name="ck_plans_dates",
        ),
    )
    op.create_index("ix_plans_chat_id", "plans", ["chat_id"])
    op.create_index("ix_plans_chat_status_updated", "plans", ["chat_id", "status", "updated_at"])
    op.create_table(
        "plan_members",
        sa.Column("plan_id", pg.UUID(), sa.ForeignKey("plans.id"), primary_key=True),
        sa.Column("user_id", pg.UUID(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("role", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("latest_actor_user_id", pg.UUID(), sa.ForeignKey("users.id")),
        sa.Column("latest_source_message_id", pg.UUID(), sa.ForeignKey("messages.id")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("status IN ('active', 'removed')", name="ck_plan_members_status"),
    )
    op.create_index("ix_plan_members_user_id", "plan_members", ["user_id"])
    op.create_table(
        "plan_items",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("plan_id", pg.UUID(), sa.ForeignKey("plans.id"), nullable=False),
        sa.Column("item_type", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("normalized_title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("assigned_user_id", pg.UUID(), sa.ForeignKey("users.id")),
        sa.Column("due_at", sa.DateTime(timezone=True)),
        sa.Column("category", sa.Text()),
        sa.Column("position", sa.Integer()),
        sa.Column("metadata", pg.JSONB(), nullable=False),
        sa.Column("source_message_id", pg.UUID(), sa.ForeignKey("messages.id")),
        sa.Column("created_by_user_id", pg.UUID(), sa.ForeignKey("users.id")),
        sa.Column("created_by_model", sa.Boolean(), nullable=False),
        sa.Column("latest_actor_user_id", pg.UUID(), sa.ForeignKey("users.id")),
        sa.Column("latest_source_message_id", pg.UUID(), sa.ForeignKey("messages.id")),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "item_type IN ('task', 'decision', 'constraint', 'activity', 'open_question', 'note')",
            name="ck_plan_items_type",
        ),
        sa.CheckConstraint(
            "(item_type = 'task' AND status IN ('open','in_progress','done','cancelled')) OR "
            "(item_type = 'decision' AND status IN ('proposed','confirmed','reversed')) OR "
            "(item_type = 'constraint' AND status IN ('active','removed')) OR "
            "(item_type = 'activity' AND status IN ('idea','shortlisted','confirmed','completed','cancelled')) OR "  # noqa: E501
            "(item_type = 'open_question' AND status IN ('open','resolved')) OR "
            "(item_type = 'note' AND status IN ('active','removed'))",
            name="ck_plan_items_status",
        ),
    )
    op.create_index("ix_plan_items_plan_id", "plan_items", ["plan_id"])
    op.create_index(
        "ix_plan_items_plan_type_status_position",
        "plan_items",
        ["plan_id", "item_type", "status", "position"],
    )


def downgrade():
    op.drop_table("plan_items")
    op.drop_table("plan_members")
    op.drop_table("plans")
