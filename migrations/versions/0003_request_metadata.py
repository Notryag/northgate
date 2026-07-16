"""Add authenticated request metadata.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "application_keys",
        sa.Column("allowed_metadata_keys", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "request_records",
        sa.Column("request_metadata", sa.JSON(), nullable=True),
    )
    op.alter_column("application_keys", "allowed_metadata_keys", server_default=None)


def downgrade() -> None:
    op.drop_column("request_records", "request_metadata")
    op.drop_column("application_keys", "allowed_metadata_keys")
