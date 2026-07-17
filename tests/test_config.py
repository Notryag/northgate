from northgate.config import Settings


def test_secrets_are_redacted() -> None:
    settings = Settings(environment="test")

    assert "northgate:northgate" not in repr(settings)
    assert settings.database_url.get_secret_value().startswith("postgresql+asyncpg://")


def test_empty_optional_environment_values_use_defaults(monkeypatch) -> None:
    monkeypatch.setenv("NORTHGATE_REQUEST_LIMIT_PER_MINUTE", "")
    monkeypatch.setenv("NORTHGATE_EXACT_CACHE_TTL_SECONDS", "")
    monkeypatch.setenv("NORTHGATE_INPUT_PRICE_MICROUSD_PER_MILLION", "")

    settings = Settings(environment="test")

    assert settings.request_limit_per_minute is None
    assert settings.exact_cache_ttl_seconds is None
    assert settings.input_price_microusd_per_million is None
