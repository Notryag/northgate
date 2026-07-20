"""Add request reservation and cache diagnostics.

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("request_records", sa.Column("cached_prompt_tokens", sa.Integer()))
    op.add_column("request_records", sa.Column("estimated_tokens", sa.Integer()))
    op.add_column("request_records", sa.Column("cache_status", sa.String(length=20)))
    op.add_column("request_records", sa.Column("error_code", sa.String(length=80)))
    op.add_column("provider_attempt_records", sa.Column("cached_prompt_tokens", sa.Integer()))


def downgrade() -> None:
    op.drop_column("provider_attempt_records", "cached_prompt_tokens")
    op.drop_column("request_records", "error_code")
    op.drop_column("request_records", "cache_status")
    op.drop_column("request_records", "estimated_tokens")
    op.drop_column("request_records", "cached_prompt_tokens")
