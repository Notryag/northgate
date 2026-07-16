"""Add versioned pricing and spend accounting.

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "gateway_policies",
        sa.Column("daily_spend_microusd", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "gateway_policies",
        sa.Column("monthly_spend_microusd", sa.BigInteger(), nullable=True),
    )
    op.create_table(
        "model_prices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("input_microusd_per_million", sa.BigInteger(), nullable=False),
        sa.Column("output_microusd_per_million", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "model", "effective_from"),
    )
    op.create_index("ix_model_prices_effective_from", "model_prices", ["effective_from"])
    op.create_index("ix_model_prices_model", "model_prices", ["model"])
    op.create_index("ix_model_prices_provider", "model_prices", ["provider"])
    op.add_column("request_records", sa.Column("price_id", sa.Uuid(), nullable=True))
    op.add_column("request_records", sa.Column("cost_microusd", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_request_records_price_id",
        "request_records",
        "model_prices",
        ["price_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_request_records_price_id", "request_records", type_="foreignkey")
    op.drop_column("request_records", "cost_microusd")
    op.drop_column("request_records", "price_id")
    op.drop_index("ix_model_prices_provider", table_name="model_prices")
    op.drop_index("ix_model_prices_model", table_name="model_prices")
    op.drop_index("ix_model_prices_effective_from", table_name="model_prices")
    op.drop_table("model_prices")
    op.drop_column("gateway_policies", "monthly_spend_microusd")
    op.drop_column("gateway_policies", "daily_spend_microusd")
