from contextlib import asynccontextmanager
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from northgate.app import create_app
from northgate.config import Settings
from northgate.db.models import Gateway, ProviderCredential


class _Session:
    def __init__(self, resources: dict[tuple[type, object], object]) -> None:
        self.resources = resources

    async def get(self, model: type, resource_id):
        return self.resources.get((model, resource_id))

    async def scalar(self, _statement):
        return None

    def add(self, resource) -> None:
        resource.id = uuid4()
        resource.created_at = datetime.now(UTC)
        self.resources[(type(resource), resource.id)] = resource

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def refresh(self, _resource) -> None:
        return None


class _Database:
    def __init__(self, resources: list[object]) -> None:
        self.resources = {(type(resource), resource.id): resource for resource in resources}

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
async def test_route_default_output_limit_can_be_created_updated_and_cleared() -> None:
    operator_key = "operator-test"
    project_id = uuid4()
    gateway = Gateway(id=uuid4(), project_id=project_id, slug="primary")
    credential = ProviderCredential(
        id=uuid4(),
        project_id=project_id,
        name="openai",
        provider="openai",
        base_url="https://api.openai.com/v1",
        adapter="openai_compatible",
        adapter_config={},
        encrypted_api_key=b"encrypted",
    )
    app = create_app(_settings(operator_key), database=_Database([gateway, credential]))
    headers = {"Authorization": f"Bearer {operator_key}"}
    payload = {
        "gateway_id": str(gateway.id),
        "provider_credential_id": str(credential.id),
        "name": "primary",
        "default_max_output_tokens": 2048,
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        invalid = await client.post(
            "/api/v1/routes",
            headers=headers,
            json={**payload, "default_max_output_tokens": 0},
        )
        created = await client.post("/api/v1/routes", headers=headers, json=payload)
        route_id = created.json()["id"]
        updated = await client.patch(
            f"/api/v1/routes/{route_id}",
            headers=headers,
            json={"default_max_output_tokens": 1024},
        )
        cleared = await client.patch(
            f"/api/v1/routes/{route_id}",
            headers=headers,
            json={"default_max_output_tokens": None},
        )

    assert invalid.status_code == 422
    assert created.status_code == 201
    assert created.json()["default_max_output_tokens"] == 2048
    assert updated.status_code == 200
    assert updated.json()["default_max_output_tokens"] == 1024
    assert cleared.status_code == 200
    assert cleared.json()["default_max_output_tokens"] is None


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
