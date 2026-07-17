"""Add provider adapter configuration.

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "provider_credentials",
        sa.Column(
            "adapter",
            sa.String(length=40),
            nullable=False,
            server_default="openai_compatible",
        ),
    )
    op.add_column(
        "provider_credentials",
        sa.Column("adapter_config", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.alter_column("provider_credentials", "adapter", server_default=None)
    op.alter_column("provider_credentials", "adapter_config", server_default=None)


def downgrade() -> None:
    op.drop_column("provider_credentials", "adapter_config")
    op.drop_column("provider_credentials", "adapter")
