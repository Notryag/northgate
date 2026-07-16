from hashlib import sha256

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from northgate.app import create_app
from northgate.config import Settings


@pytest.mark.anyio
async def test_health_endpoints_include_request_id() -> None:
    app = create_app(Settings(environment="test"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        live = await client.get("/health/live")
        ready = await client.get("/health/ready")

    assert live.status_code == 200
    assert live.json() == {"status": "ok"}
    assert live.headers["Northgate-Request-Id"].startswith("req_")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}


@pytest.mark.anyio
async def test_valid_request_id_is_preserved() -> None:
    request_id = "req_test-request-123"
    app = create_app(Settings(environment="test"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health/live", headers={"Northgate-Request-Id": request_id})

    assert response.headers["Northgate-Request-Id"] == request_id


@pytest.mark.anyio
async def test_invalid_request_id_is_replaced() -> None:
    app = create_app(Settings(environment="test"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health/live", headers={"Northgate-Request-Id": "invalid"})

    assert response.headers["Northgate-Request-Id"].startswith("req_")
    assert response.headers["Northgate-Request-Id"] != "invalid"


@pytest.mark.anyio
async def test_application_key_cannot_access_operator_analytics() -> None:
    operator_key = "operator-test"
    app = create_app(
        Settings(
            environment="test",
            operator_key_sha256=SecretStr(sha256(operator_key.encode()).hexdigest()),
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unauthorized = await client.get(
            "/api/v1/usage/summary", headers={"Authorization": "Bearer application-key"}
        )
        operator = await client.get(
            "/api/v1/usage/summary", headers={"Authorization": f"Bearer {operator_key}"}
        )

    assert unauthorized.status_code == 401
    assert operator.status_code == 503


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
