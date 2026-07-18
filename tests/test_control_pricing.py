from contextlib import asynccontextmanager
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from northgate.app import create_app
from northgate.config import Settings


class _Scalars:
    def __init__(self, resources: list) -> None:
        self.resources = resources

    def all(self) -> list:
        return self.resources


class _Session:
    def __init__(self, resources: list) -> None:
        self.resources = resources

    async def scalars(self, _statement) -> _Scalars:
        return _Scalars(self.resources)

    async def scalar(self, _statement):
        return self.resources[0].id if self.resources else None

    def add(self, resource) -> None:
        resource.id = uuid4()
        resource.created_at = datetime.now(UTC)
        self.resources.append(resource)

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def refresh(self, _resource) -> None:
        return None


class _Database:
    def __init__(self) -> None:
        self.resources: list = []

    @asynccontextmanager
    async def sessions(self):
        yield _Session(self.resources)


def _settings(operator_key: str) -> Settings:
    return Settings(
        environment="test",
        operator_key_sha256=SecretStr(sha256(operator_key.encode()).hexdigest()),
        routing_source="configuration",
        usage_persistence_enabled=False,
        request_limit_per_minute=None,
        concurrency_limit=None,
        token_limit_per_day=None,
        daily_spend_limit_microusd=None,
        monthly_spend_limit_microusd=None,
        exact_cache_ttl_seconds=None,
        route_health_enabled=False,
    )


@pytest.mark.anyio
async def test_model_prices_are_operator_only_effective_dated_records() -> None:
    operator_key = "operator-test"
    app = create_app(_settings(operator_key), database=_Database())
    authorized = {"Authorization": f"Bearer {operator_key}"}
    payload = {
        "provider": " openai ",
        "model": " model-a ",
        "effective_from": "2026-07-18T00:00:00Z",
        "input_microusd_per_million": 1_250_000,
        "output_microusd_per_million": 5_000_000,
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unauthorized = await client.get(
            "/api/v1/model-prices",
            headers={"Authorization": "Bearer application-key"},
        )
        naive_time = await client.post(
            "/api/v1/model-prices",
            headers=authorized,
            json={**payload, "effective_from": "2026-07-18T00:00:00"},
        )
        created = await client.post("/api/v1/model-prices", headers=authorized, json=payload)
        conflict = await client.post("/api/v1/model-prices", headers=authorized, json=payload)
        listed = await client.get("/api/v1/model-prices", headers=authorized)

    assert unauthorized.status_code == 401
    assert naive_time.status_code == 422
    assert created.status_code == 201
    assert created.json()["provider"] == "openai"
    assert created.json()["model"] == "model-a"
    assert created.json()["input_microusd_per_million"] == 1_250_000
    assert conflict.status_code == 409
    assert listed.status_code == 200
    assert listed.json() == [created.json()]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
