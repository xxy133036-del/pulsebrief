"""Add verified email identity and email reminder delivery support.

Revision ID: c2b85a4170d1
Revises: 9ac4f6d28031
Create Date: 2026-10-14 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c2b85a4170d1"
down_revision: str | None = "9ac4f6d28031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("email_verified", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("users", "email_verified")
