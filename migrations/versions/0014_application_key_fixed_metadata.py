"""Bind fixed routing metadata to application keys.

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "application_keys",
        sa.Column("fixed_metadata", sa.JSON(), server_default="{}", nullable=False),
    )
    op.add_column(
        "application_keys",
        sa.Column(
            "metadata_routing_mode",
            sa.String(length=16),
            server_default="legacy",
            nullable=False,
        ),
    )
    op.alter_column("application_keys", "fixed_metadata", server_default=None)
    op.alter_column("application_keys", "metadata_routing_mode", server_default="trusted")


def downgrade() -> None:
    op.drop_column("application_keys", "metadata_routing_mode")
    op.drop_column("application_keys", "fixed_metadata")
