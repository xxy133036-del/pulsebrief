"""Remove the unused mobile push device registry from the website project.

Revision ID: e83d48a117ce
Revises: c2b85a4170d1
Create Date: 2026-10-14 13:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e83d48a117ce"
down_revision: str | None = "c2b85a4170d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_table("device_installations")


def downgrade() -> None:
    op.create_table(
        "device_installations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("installation_id", sa.String(length=64), nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("app_version", sa.String(length=40), nullable=False),
        sa.Column("push_provider", sa.String(length=32), nullable=False),
        sa.Column("push_token_hash", sa.String(length=128), nullable=True),
        sa.Column("push_token", sa.Text(), nullable=True),
        sa.Column("notifications_enabled", sa.Boolean(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("installation_id"),
    )
    op.create_index(
        "ix_device_installations_installation_id",
        "device_installations",
        ["installation_id"],
        unique=True,
    )
    op.create_index("ix_device_installations_user_id", "device_installations", ["user_id"])
