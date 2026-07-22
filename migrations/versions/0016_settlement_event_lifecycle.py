"""Version settlement payloads and index the worker queue.

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE settlement_events
        SET payload = (payload::jsonb || '{"schema_version": 1}'::jsonb)::json
        WHERE NOT (payload::jsonb ? 'schema_version')
        """
    )
    op.create_index(
        "ix_settlement_events_worker_queue",
        "settlement_events",
        ["available_at", "created_at", "locked_at"],
        postgresql_where=sa.text("status IN ('pending', 'retry', 'processing')"),
    )


def downgrade() -> None:
    op.drop_index("ix_settlement_events_worker_queue", table_name="settlement_events")
    op.execute(
        """
        UPDATE settlement_events
        SET payload = (payload::jsonb - 'schema_version')::json
        """
    )
