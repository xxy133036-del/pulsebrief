"""Default subscriptions to detailed summaries.

Revision ID: a26c9d51e804
Revises: f79a1e5c4d20
Create Date: 2026-10-20 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a26c9d51e804"
down_revision: str | None = "f79a1e5c4d20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Earlier "concise" values were the system default; users can still opt back in.
    op.execute("UPDATE subscriptions SET detail_level = 'detailed' WHERE detail_level = 'concise'")


def downgrade() -> None:
    # Keep user-selected detail preferences when rolling back application code.
    pass
