"""Allow multiple idempotent settlement events per request.

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "settlement_events",
        sa.Column("event_key", sa.String(length=160), server_default="terminal", nullable=False),
    )
    op.drop_constraint("settlement_events_request_id_key", "settlement_events", type_="unique")
    op.create_unique_constraint(
        "uq_settlement_events_request_event_key",
        "settlement_events",
        ["request_id", "event_key"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_settlement_events_request_event_key",
        "settlement_events",
        type_="unique",
    )
    op.create_unique_constraint(
        "settlement_events_request_id_key",
        "settlement_events",
        ["request_id"],
    )
    op.drop_column("settlement_events", "event_key")
