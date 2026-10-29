"""Add feedback review states and user-visible replies.

Revision ID: f79a1e5c4d20
Revises: e83d48a117ce
Create Date: 2026-10-18 23:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f79a1e5c4d20"
down_revision: str | None = "b41d9c7e2f31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("feedback") as batch_op:
        batch_op.add_column(
            sa.Column("status", sa.String(length=16), nullable=False, server_default="open")
        )
        batch_op.add_column(
            sa.Column("reply", sa.Text(), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column(
                "handled_by_user_id",
                sa.String(length=36),
                sa.ForeignKey(
                    "users.id", ondelete="SET NULL", name="fk_feedback_handled_by_user_id_users"
                ),
                nullable=True,
            )
        )
        batch_op.add_column(sa.Column("updated_at", sa.DateTime(), nullable=True))
        batch_op.create_index("ix_feedback_status", ["status"], unique=False)
    op.execute("UPDATE feedback SET updated_at = created_at WHERE updated_at IS NULL")


def downgrade() -> None:
    with op.batch_alter_table("feedback") as batch_op:
        batch_op.drop_index("ix_feedback_status")
        batch_op.drop_column("updated_at")
        batch_op.drop_column("handled_by_user_id")
        batch_op.drop_column("reply")
        batch_op.drop_column("status")
