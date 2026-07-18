from contextlib import asynccontextmanager
from hashlib import sha256
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from northgate.app import create_app
from northgate.config import Settings


class _Result:
    def all(self) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                tenant_id="tenant-a",
                requests=4,
                successful=3,
                errors=1,
                in_flight=0,
                total_tokens=120,
                cost_microusd=45,
                average_latency_ms=12.345,
            ),
            SimpleNamespace(
                tenant_id=None,
                requests=1,
                successful=0,
                errors=0,
                in_flight=1,
                total_tokens=0,
                cost_microusd=0,
                average_latency_ms=None,
            ),
        ]


class _Session:
    async def execute(self, _statement) -> _Result:
        return _Result()


class _Database:
    @asynccontextmanager
    async def sessions(self):
        yield _Session()


@pytest.mark.anyio
async def test_tenant_usage_returns_aggregates_without_user_or_run_metadata() -> None:
    operator_key = "operator-test"
    app = create_app(
        Settings(
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
    )
    app.state.database = _Database()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unauthorized = await client.get(
            "/api/v1/usage/tenants",
            headers={"Authorization": "Bearer application-key"},
        )
        response = await client.get(
            "/api/v1/usage/tenants",
            headers={"Authorization": f"Bearer {operator_key}"},
        )

    assert unauthorized.status_code == 401
    assert response.status_code == 200
    payload = response.json()
    assert payload["tenants"] == [
        {
            "tenant_id": "tenant-a",
            "requests": 4,
            "successful_requests": 3,
            "error_requests": 1,
            "in_flight_requests": 0,
            "success_rate_percent": 75.0,
            "total_tokens": 120,
            "cost_microusd": 45,
            "average_latency_ms": 12.35,
        },
        {
            "tenant_id": None,
            "requests": 1,
            "successful_requests": 0,
            "error_requests": 0,
            "in_flight_requests": 1,
            "success_rate_percent": 0,
            "total_tokens": 0,
            "cost_microusd": 0,
            "average_latency_ms": None,
        },
    ]
    assert "user_id" not in response.text
    assert "run_id" not in response.text


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
