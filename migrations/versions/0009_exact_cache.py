"""Add gateway exact cache policy.

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "gateway_policies",
        sa.Column("exact_cache_ttl_seconds", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_gateway_policies_exact_cache_ttl_positive",
        "gateway_policies",
        "exact_cache_ttl_seconds IS NULL OR exact_cache_ttl_seconds > 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_gateway_policies_exact_cache_ttl_positive",
        "gateway_policies",
        type_="check",
    )
    op.drop_column("gateway_policies", "exact_cache_ttl_seconds")
