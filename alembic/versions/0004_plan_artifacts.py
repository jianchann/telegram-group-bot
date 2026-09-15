"""URL artifacts saved to plans."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "0004_plan_artifacts"
down_revision = "0003_plans"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "plan_artifacts",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("plan_id", pg.UUID(), sa.ForeignKey("plans.id"), nullable=False),
        sa.Column("artifact_type", sa.Text(), nullable=False),
        sa.Column("title", sa.Text()),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("normalized_url", sa.Text(), nullable=False),
        sa.Column("category", sa.Text()),
        sa.Column("metadata", pg.JSONB(), nullable=False),
        sa.Column("source_message_id", pg.UUID(), sa.ForeignKey("messages.id")),
        sa.Column("created_by_user_id", pg.UUID(), sa.ForeignKey("users.id")),
        sa.Column("latest_actor_user_id", pg.UUID(), sa.ForeignKey("users.id")),
        sa.Column("latest_source_message_id", pg.UUID(), sa.ForeignKey("messages.id")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("artifact_type = 'url'", name="ck_plan_artifacts_type"),
        sa.UniqueConstraint("plan_id", "normalized_url", name="uq_plan_artifacts_plan_url"),
    )
    op.create_index("ix_plan_artifacts_plan_id", "plan_artifacts", ["plan_id"])
    op.create_index(
        "ix_plan_artifacts_plan_category_created",
        "plan_artifacts",
        ["plan_id", "category", "created_at"],
    )


def downgrade():
    op.drop_table("plan_artifacts")
