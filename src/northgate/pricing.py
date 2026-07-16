from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select

from northgate.config import Settings
from northgate.db.database import Database
from northgate.db.models import ModelPrice


@dataclass(frozen=True)
class PriceQuote:
    price_id: UUID | None
    input_microusd_per_million: int
    output_microusd_per_million: int

    def cost(self, input_tokens: int, output_tokens: int) -> int:
        numerator = (
            input_tokens * self.input_microusd_per_million
            + output_tokens * self.output_microusd_per_million
        )
        return (numerator + 999_999) // 1_000_000

    def usage_cost(self, prompt_tokens: int | None, completion_tokens: int | None) -> int | None:
        if prompt_tokens is None or completion_tokens is None:
            return None
        return self.cost(prompt_tokens, completion_tokens)


class PricingRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def resolve(self, provider: str, model: str, at: datetime) -> PriceQuote | None:
        async with self.database.sessions() as session:
            price = await session.scalar(
                select(ModelPrice)
                .where(
                    ModelPrice.provider == provider,
                    ModelPrice.model == model,
                    ModelPrice.effective_from <= at,
                )
                .order_by(ModelPrice.effective_from.desc())
                .limit(1)
            )
        if price is None:
            return None
        return PriceQuote(
            price_id=price.id,
            input_microusd_per_million=price.input_microusd_per_million,
            output_microusd_per_million=price.output_microusd_per_million,
        )


def configured_price(settings: Settings) -> PriceQuote | None:
    input_price = settings.input_price_microusd_per_million
    output_price = settings.output_price_microusd_per_million
    if input_price is None or output_price is None:
        return None
    return PriceQuote(
        price_id=None,
        input_microusd_per_million=input_price,
        output_microusd_per_million=output_price,
    )
