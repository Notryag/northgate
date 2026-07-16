from northgate.config import Settings


def test_secrets_are_redacted() -> None:
    settings = Settings(environment="test")

    assert "northgate:northgate" not in repr(settings)
    assert settings.database_url.get_secret_value().startswith("postgresql+asyncpg://")
