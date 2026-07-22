"""Add durable settlement outbox.

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "settlement_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(length=500)),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("locked_at", sa.DateTime(timezone=True)),
        sa.Column("database_settled_at", sa.DateTime(timezone=True)),
        sa.Column("policy_settled_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id"),
    )
    op.create_index("ix_settlement_events_request_id", "settlement_events", ["request_id"])
    op.create_index("ix_settlement_events_status", "settlement_events", ["status"])
    op.create_index("ix_settlement_events_available_at", "settlement_events", ["available_at"])


def downgrade() -> None:
    op.drop_index("ix_settlement_events_available_at", table_name="settlement_events")
    op.drop_index("ix_settlement_events_status", table_name="settlement_events")
    op.drop_index("ix_settlement_events_request_id", table_name="settlement_events")
    op.drop_table("settlement_events")
