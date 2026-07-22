"""Preserve metadata trust classes in the request ledger.

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("request_records", sa.Column("request_metadata_trust", sa.JSON()))


def downgrade() -> None:
    op.drop_column("request_records", "request_metadata_trust")
