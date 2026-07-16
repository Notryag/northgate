from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="NORTHGATE_",
        extra="ignore",
    )

    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    host: str = "127.0.0.1"
    port: int = 8080
    database_url: SecretStr = SecretStr(
        "postgresql+asyncpg://northgate:northgate@localhost:5433/northgate"
    )
    redis_url: SecretStr = SecretStr("redis://localhost:6380/0")
    routing_source: Literal["configuration", "database"] = "configuration"
    usage_persistence_enabled: bool = False
    gateway_slug: str = "default"
    allowed_metadata_keys: str = "tenant_id,user_id,run_id,environment"
    request_limit_per_minute: int | None = Field(default=None, gt=0)
    concurrency_limit: int | None = Field(default=None, gt=0)
    token_limit_per_day: int | None = Field(default=None, gt=0)
    daily_spend_limit_microusd: int | None = Field(default=None, gt=0)
    monthly_spend_limit_microusd: int | None = Field(default=None, gt=0)
    input_price_microusd_per_million: int | None = Field(default=None, ge=0)
    output_price_microusd_per_million: int | None = Field(default=None, ge=0)
    price_provider: str = "openai"
    price_model: str | None = None
    policy_default_max_output_tokens: int = Field(default=4096, gt=0)
    concurrency_lease_seconds: int = Field(default=300, ge=30)
    application_key_sha256: SecretStr | None = None
    provider_base_url: str = "https://api.openai.com/v1"
    provider_api_key: SecretStr | None = None
    provider_connect_timeout_seconds: float = Field(default=10.0, gt=0)
    provider_read_timeout_seconds: float = Field(default=300.0, gt=0)
    provider_write_timeout_seconds: float = Field(default=30.0, gt=0)
    provider_pool_timeout_seconds: float = Field(default=10.0, gt=0)
    credential_encryption_key: SecretStr | None = None
    operator_key_sha256: SecretStr | None = None
    console_directory: Path = Path("apps/console/dist")


@lru_cache
def get_settings() -> Settings:
    return Settings()
