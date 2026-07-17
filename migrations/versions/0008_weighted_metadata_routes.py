"""Add weighted and metadata-based route selection.

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("routes_gateway_id_priority_key", "routes", type_="unique")
    op.add_column(
        "routes",
        sa.Column("weight", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "routes",
        sa.Column("match_metadata", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.alter_column("routes", "weight", server_default=None)
    op.alter_column("routes", "match_metadata", server_default=None)
    op.create_check_constraint("ck_routes_weight_positive", "routes", "weight > 0")


def downgrade() -> None:
    op.drop_constraint("ck_routes_weight_positive", "routes", type_="check")
    op.drop_column("routes", "match_metadata")
    op.drop_column("routes", "weight")
    op.create_unique_constraint(
        "routes_gateway_id_priority_key",
        "routes",
        ["gateway_id", "priority"],
    )
