from northgate.pricing import PriceQuote


def test_price_quote_uses_integer_microusd_and_rounds_up() -> None:
    quote = PriceQuote(
        price_id=None,
        input_microusd_per_million=1_000_000,
        output_microusd_per_million=2_000_000,
    )

    assert quote.cost(10, 4) == 18
    assert quote.cost(0, 1) == 2
    assert quote.cost(0, 0) == 0
