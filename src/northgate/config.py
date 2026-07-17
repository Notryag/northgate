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
    metrics_enabled: bool = False
    metrics_key_sha256: SecretStr | None = None
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
    exact_cache_ttl_seconds: int | None = Field(default=None, ge=1, le=86400)
    cache_max_entry_bytes: int = Field(default=1048576, ge=1024, le=16777216)
    input_price_microusd_per_million: int | None = Field(default=None, ge=0)
    output_price_microusd_per_million: int | None = Field(default=None, ge=0)
    price_provider: str = "openai"
    price_model: str | None = None
    policy_default_max_output_tokens: int = Field(default=4096, gt=0)
    concurrency_lease_seconds: int = Field(default=300, ge=30)
    application_key_sha256: SecretStr | None = None
    provider_base_url: str = "https://api.openai.com/v1"
    provider_api_key: SecretStr | None = None
    provider_adapter: Literal["openai_compatible", "azure_openai"] = "openai_compatible"
    provider_api_version: str | None = None
    provider_connect_timeout_seconds: float = Field(default=10.0, gt=0)
    provider_read_timeout_seconds: float = Field(default=300.0, gt=0)
    provider_write_timeout_seconds: float = Field(default=30.0, gt=0)
    provider_pool_timeout_seconds: float = Field(default=10.0, gt=0)
    provider_max_retries: int = Field(default=0, ge=0, le=5)
    provider_retry_status_codes: str = "429,500,502,503,504"
    provider_retry_backoff_ms: int = Field(default=100, ge=0, le=5000)
    fallback_provider_name: str | None = None
    fallback_provider_base_url: str | None = None
    fallback_provider_api_key: SecretStr | None = None
    fallback_provider_adapter: Literal["openai_compatible", "azure_openai"] = "openai_compatible"
    fallback_provider_api_version: str | None = None
    fallback_provider_max_retries: int = Field(default=0, ge=0, le=5)
    route_health_enabled: bool = False
    route_health_failure_threshold: int = Field(default=3, ge=1, le=100)
    route_health_recovery_seconds: int = Field(default=30, ge=1, le=3600)
    route_health_failure_status_codes: str = "500,502,503,504"
    credential_encryption_key: SecretStr | None = None
    operator_key_sha256: SecretStr | None = None
    console_directory: Path = Path("apps/console/dist")


@lru_cache
def get_settings() -> Settings:
    return Settings()
