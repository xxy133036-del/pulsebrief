"""Store original-journalist bylines for licensed articles.

Revision ID: c72a4d90be31
Revises: a26c9d51e804
Create Date: 2026-10-20 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c72a4d90be31"
down_revision: str | None = "a26c9d51e804"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("news_articles") as batch_op:
        batch_op.add_column(
            sa.Column("author", sa.String(length=256), nullable=False, server_default="")
        )


def downgrade() -> None:
    with op.batch_alter_table("news_articles") as batch_op:
        batch_op.drop_column("author")
