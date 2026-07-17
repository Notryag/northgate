"""Add route health policy.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "routes",
        sa.Column("health_failure_threshold", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "routes",
        sa.Column("health_recovery_seconds", sa.Integer(), nullable=False, server_default="30"),
    )
    op.add_column(
        "routes",
        sa.Column(
            "health_failure_status_codes",
            sa.JSON(),
            nullable=False,
            server_default="[500, 502, 503, 504]",
        ),
    )
    op.alter_column("routes", "health_failure_threshold", server_default=None)
    op.alter_column("routes", "health_recovery_seconds", server_default=None)
    op.alter_column("routes", "health_failure_status_codes", server_default=None)


def downgrade() -> None:
    op.drop_column("routes", "health_failure_status_codes")
    op.drop_column("routes", "health_recovery_seconds")
    op.drop_column("routes", "health_failure_threshold")
