"""Store aggregate model token usage for generation tasks.

Revision ID: b41d9c7e2f31
Revises: e83d48a117ce
Create Date: 2026-10-14 19:50:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b41d9c7e2f31"
down_revision: str | None = "e83d48a117ce"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "generation_tasks",
        sa.Column("token_usage", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("generation_tasks", "token_usage")
