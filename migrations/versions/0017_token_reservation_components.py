"""Explain token reservations and add route output defaults.

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("routes", sa.Column("default_max_output_tokens", sa.Integer()))
    op.create_check_constraint(
        "ck_routes_default_max_output_tokens_positive",
        "routes",
        "default_max_output_tokens IS NULL OR default_max_output_tokens > 0",
    )
    op.add_column("request_records", sa.Column("estimated_prompt_tokens", sa.Integer()))
    op.add_column("request_records", sa.Column("reserved_output_tokens", sa.Integer()))
    op.add_column("request_records", sa.Column("attempt_multiplier", sa.Integer()))
    op.add_column("request_records", sa.Column("reservation_margin_tokens", sa.Integer()))
    op.add_column("request_records", sa.Column("reserved_total_tokens", sa.Integer()))
    op.add_column("request_records", sa.Column("token_estimator", sa.String(length=80)))
    op.add_column("request_records", sa.Column("output_limit_source", sa.String(length=20)))


def downgrade() -> None:
    op.drop_column("request_records", "output_limit_source")
    op.drop_column("request_records", "token_estimator")
    op.drop_column("request_records", "reserved_total_tokens")
    op.drop_column("request_records", "reservation_margin_tokens")
    op.drop_column("request_records", "attempt_multiplier")
    op.drop_column("request_records", "reserved_output_tokens")
    op.drop_column("request_records", "estimated_prompt_tokens")
    op.drop_constraint("ck_routes_default_max_output_tokens_positive", "routes", type_="check")
    op.drop_column("routes", "default_max_output_tokens")
