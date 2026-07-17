"""Add route retry policy and provider attempt records.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "routes", sa.Column("max_retries", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "routes",
        sa.Column(
            "retry_status_codes",
            sa.JSON(),
            nullable=False,
            server_default="[429, 500, 502, 503, 504]",
        ),
    )
    op.alter_column("routes", "max_retries", server_default=None)
    op.alter_column("routes", "retry_status_codes", server_default=None)
    op.create_table(
        "provider_attempt_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("attempt_index", sa.Integer(), nullable=False),
        sa.Column("route_id", sa.Uuid(), nullable=True),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("price_id", sa.Uuid(), nullable=True),
        sa.Column("outcome", sa.String(length=40), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("provider_request_id", sa.String(length=200), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_microusd", sa.BigInteger(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["price_id"], ["model_prices.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["request_id"], ["request_records.request_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["route_id"], ["routes.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id", "attempt_index"),
    )
    op.create_index(
        "ix_provider_attempt_records_request_id", "provider_attempt_records", ["request_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_provider_attempt_records_request_id", table_name="provider_attempt_records")
    op.drop_table("provider_attempt_records")
    op.drop_column("routes", "retry_status_codes")
    op.drop_column("routes", "max_retries")
