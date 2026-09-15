"""Rolling chat summary state."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "0005_chat_summaries"
down_revision = "0004_plan_artifacts"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "chat_summaries",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("chat_id", pg.UUID(), sa.ForeignKey("chats.id"), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("through_message_id", pg.UUID(), sa.ForeignKey("messages.id")),
        sa.Column("summary_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("chat_id", name="uq_chat_summaries_chat_id"),
    )


def downgrade():
    op.drop_table("chat_summaries")
