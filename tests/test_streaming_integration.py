import asyncio
import os
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import delete, select, text
from sqlalchemy.exc import SQLAlchemyError

from northgate.app import create_app
from northgate.config import Settings
from northgate.db.database import Database
from northgate.db.models import ProviderAttemptRecord, RequestRecord, SettlementEvent
from northgate.metrics import Metrics
from northgate.reconcile import reconcile

pytestmark = pytest.mark.integration


def _integration_store_unavailable(reason: str) -> None:
    if os.environ.get("NORTHGATE_REQUIRE_INTEGRATION_STORES") == "1":
        pytest.fail(reason)
    pytest.skip(reason)


class TerminalThenHangStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = asyncio.Event()
        self.release = asyncio.Event()

    async def __aiter__(self):
        yield b'data: {"choices": [{"delta": {"content": "ok"}}]}\n\n'
        yield (
            b'data: {"usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}\n\n'
        )
        yield b"data: [DONE]\n\n"
        await self.release.wait()

    async def aclose(self) -> None:
        self.closed.set()
        self.release.set()


class TerminalThenBlockedCloseStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.close_entered = asyncio.Event()
        self.release_close = asyncio.Event()

    async def __aiter__(self):
        yield (
            b'data: {"usage": {"prompt_tokens": 2449, '
            b'"completion_tokens": 14, "total_tokens": 2463}}\n\n'
        )
        yield b"data: [DONE]\n\n"

    async def aclose(self) -> None:
        self.close_entered.set()
        await self.release_close.wait()


@pytest.mark.anyio
async def test_real_storage_sequential_streams_release_policy_leases() -> None:
    database_url = os.environ.get(
        "NORTHGATE_TEST_DATABASE_URL",
        "postgresql+asyncpg://northgate:northgate@127.0.0.1:5433/northgate",
    )
    redis_url = os.environ.get(
        "NORTHGATE_TEST_REDIS_URL",
        "redis://127.0.0.1:6380/15",
    )
    database = Database(database_url)
    redis = Redis.from_url(redis_url)
    try:
        if not await database.ping():
            _integration_store_unavailable("PostgreSQL is not available")
        await redis.ping()
        async with database.sessions() as session:
            await session.execute(text("SELECT 1 FROM request_records LIMIT 1"))
    except (RedisError, SQLAlchemyError, OSError):
        await database.close()
        await redis.aclose()
        _integration_store_unavailable(
            "Northgate integration stores are not available or are not migrated"
        )

    application_key = f"ng_integration_{uuid4().hex}"
    gateway_slug = f"integration-{uuid4().hex[:12]}"
    request_ids: list[str] = []
    streams: list[TerminalThenHangStream] = []

    async def upstream(_request: httpx.Request) -> httpx.Response:
        stream = TerminalThenHangStream()
        streams.append(stream)
        return httpx.Response(
            200,
            stream=stream,
            headers={"Content-Type": "text/event-stream"},
        )

    upstream_client = AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(
        Settings(
            environment="test",
            application_key_sha256=SecretStr(sha256(application_key.encode()).hexdigest()),
            provider_base_url="https://provider.integration/v1",
            provider_api_key=SecretStr("integration-provider-secret"),
            usage_persistence_enabled=True,
            settlement_outbox_enabled=True,
            concurrency_limit=1,
            concurrency_lease_seconds=30,
            routing_source="configuration",
            gateway_slug=gateway_slug,
            database_url=SecretStr(database_url),
            redis_url=SecretStr(redis_url),
        ),
        upstream_client=upstream_client,
        database=database,
        redis=redis,
    )
    path = f"/v1/gateways/{gateway_slug}/openai/chat/completions"
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for _ in range(3):
                response = await client.post(
                    path,
                    json={"model": "gpt-integration", "stream": True},
                    headers={"Authorization": f"Bearer {application_key}"},
                )
                assert response.status_code == 200
                assert response.content.endswith(b"data: [DONE]\n\n")
                request_ids.append(response.headers["Northgate-Request-Id"])

        async with database.sessions() as session:
            records = list(
                (
                    await session.scalars(
                        select(RequestRecord).where(RequestRecord.request_id.in_(request_ids))
                    )
                ).all()
            )
            attempts = list(
                (
                    await session.scalars(
                        select(ProviderAttemptRecord).where(
                            ProviderAttemptRecord.request_id.in_(request_ids)
                        )
                    )
                ).all()
            )
            settlement_events = list(
                (
                    await session.scalars(
                        select(SettlementEvent).where(SettlementEvent.request_id.in_(request_ids))
                    )
                ).all()
            )

        assert len(records) == 3
        assert {record.outcome for record in records} == {"succeeded"}
        assert {record.total_tokens for record in records} == {5}
        assert len(attempts) == 3
        assert {attempt.outcome for attempt in attempts} == {"succeeded"}
        assert len(settlement_events) == 3
        assert {event.status for event in settlement_events} == {"completed"}
        assert all(event.database_settled_at is not None for event in settlement_events)
        assert all(event.policy_settled_at is not None for event in settlement_events)

        policy_key = f"northgate:policy:{{{gateway_slug}}}:concurrency"
        assert await redis.zcard(policy_key) == 0
        assert all(stream.closed.is_set() for stream in streams)
    finally:
        async with database.sessions() as session:
            await session.execute(
                delete(SettlementEvent).where(SettlementEvent.request_id.in_(request_ids))
            )
            await session.execute(
                delete(ProviderAttemptRecord).where(
                    ProviderAttemptRecord.request_id.in_(request_ids)
                )
            )
            await session.execute(
                delete(RequestRecord).where(RequestRecord.request_id.in_(request_ids))
            )
            await session.commit()
        await redis.delete(f"northgate:policy:{{{gateway_slug}}}:concurrency")
        await upstream_client.aclose()
        await database.close()
        await redis.aclose()


@pytest.mark.anyio
async def test_direct_cancellation_after_terminal_event_preserves_durable_usage() -> None:
    database_url = os.environ.get(
        "NORTHGATE_TEST_DATABASE_URL",
        "postgresql+asyncpg://northgate:northgate@127.0.0.1:5433/northgate",
    )
    redis_url = os.environ.get(
        "NORTHGATE_TEST_REDIS_URL",
        "redis://127.0.0.1:6380/15",
    )
    database = Database(database_url)
    redis = Redis.from_url(redis_url)
    try:
        if not await database.ping():
            _integration_store_unavailable("PostgreSQL is not available")
        await redis.ping()
        async with database.sessions() as session:
            await session.execute(text("SELECT 1 FROM settlement_events LIMIT 1"))
    except (RedisError, SQLAlchemyError, OSError):
        await database.close()
        await redis.aclose()
        _integration_store_unavailable(
            "Northgate integration stores are not available or are not migrated"
        )

    application_key = f"ng_integration_{uuid4().hex}"
    gateway_slug = f"integration-cancel-{uuid4().hex[:12]}"
    run_id = f"run-{uuid4().hex}"
    policy_key = f"northgate:policy:{{{gateway_slug}}}:concurrency"
    stream = TerminalThenBlockedCloseStream()

    async def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=stream,
            headers={"Content-Type": "text/event-stream"},
        )

    upstream_client = AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(
        Settings(
            environment="test",
            application_key_sha256=SecretStr(sha256(application_key.encode()).hexdigest()),
            provider_base_url="https://provider.integration/v1",
            provider_api_key=SecretStr("integration-provider-secret"),
            usage_persistence_enabled=True,
            settlement_outbox_enabled=True,
            concurrency_limit=1,
            concurrency_lease_seconds=30,
            routing_source="configuration",
            gateway_slug=gateway_slug,
            database_url=SecretStr(database_url),
            redis_url=SecretStr(redis_url),
        ),
        upstream_client=upstream_client,
        database=database,
        redis=redis,
    )
    path = f"/v1/gateways/{gateway_slug}/openai/chat/completions"
    request_sent = False
    request_id: str | None = None

    async def receive() -> dict[str, object]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {
                "type": "http.request",
                "body": b'{"model":"gpt-integration","stream":true}',
                "more_body": False,
            }
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        nonlocal request_id
        if message["type"] != "http.response.start":
            return
        headers = dict(message["headers"])
        request_id = headers[b"northgate-request-id"].decode()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [
            (b"host", b"test"),
            (b"authorization", f"Bearer {application_key}".encode()),
            (b"content-type", b"application/json"),
            (b"northgate-metadata", f'{{"run_id":"{run_id}"}}'.encode()),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
    }

    task = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(stream.close_entered.wait(), timeout=2)
        assert request_id is not None
        async with database.sessions() as session:
            handed_off_event = await session.scalar(
                select(SettlementEvent).where(SettlementEvent.request_id == request_id)
            )
        assert handed_off_event is not None
        assert handed_off_event.status == "pending"
        assert handed_off_event.database_settled_at is None

        task.cancel()
        await asyncio.sleep(0)
        stream.release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)

        async with database.sessions() as session:
            request_record = await session.get(RequestRecord, request_id)
            attempt_record = await session.scalar(
                select(ProviderAttemptRecord).where(ProviderAttemptRecord.request_id == request_id)
            )
            event = await session.scalar(
                select(SettlementEvent).where(SettlementEvent.request_id == request_id)
            )
        assert request_record is not None and request_record.outcome == "succeeded"
        assert request_record.prompt_tokens == 2449
        assert request_record.completion_tokens == 14
        assert request_record.total_tokens == 2463
        assert request_record.request_metadata == {"run_id": run_id}
        assert request_record.request_metadata_trust == {"run_id": "untrusted"}
        assert attempt_record is not None and attempt_record.outcome == "succeeded"
        assert attempt_record.total_tokens == 2463
        assert event is not None and event.status == "completed"
        assert event.database_settled_at is not None
        assert event.policy_settled_at is not None
        assert await redis.zcard(policy_key) == 0
    finally:
        stream.release_close.set()
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if request_id is not None:
            async with database.sessions() as session:
                await session.execute(
                    delete(SettlementEvent).where(SettlementEvent.request_id == request_id)
                )
                await session.execute(
                    delete(ProviderAttemptRecord).where(
                        ProviderAttemptRecord.request_id == request_id
                    )
                )
                await session.execute(
                    delete(RequestRecord).where(RequestRecord.request_id == request_id)
                )
                await session.commit()
        await redis.delete(policy_key, f"{policy_key}:started")
        await upstream_client.aclose()
        await database.close()
        await redis.aclose()


@pytest.mark.anyio
async def test_reconciliation_previews_then_recovers_stale_records_and_lease() -> None:
    database_url = os.environ.get(
        "NORTHGATE_TEST_DATABASE_URL",
        "postgresql+asyncpg://northgate:northgate@127.0.0.1:5433/northgate",
    )
    redis_url = os.environ.get(
        "NORTHGATE_TEST_REDIS_URL",
        "redis://127.0.0.1:6380/15",
    )
    database = Database(database_url)
    redis = Redis.from_url(redis_url)
    request_id = f"req_reconcile_{uuid4().hex}"
    gateway_slug = f"reconcile-{uuid4().hex[:12]}"
    policy_key = f"northgate:policy:{{{gateway_slug}}}:concurrency"
    started_key = f"{policy_key}:started"
    active_request_id = f"req_active_{uuid4().hex}"
    old = datetime.now(UTC) - timedelta(hours=1)
    record_created = False
    try:
        if not await database.ping():
            _integration_store_unavailable("PostgreSQL is not available")
        await redis.ping()
        async with database.sessions() as session:
            await session.execute(text("SELECT 1 FROM request_records LIMIT 1"))
    except (RedisError, SQLAlchemyError, OSError):
        await database.close()
        await redis.aclose()
        _integration_store_unavailable(
            "Northgate integration stores are not available or are not migrated"
        )

    try:
        async with database.sessions() as session:
            request_record = RequestRecord(
                request_id=request_id,
                provider="integration",
                model="gpt-integration",
                estimated_tokens=10,
                cache_status="bypass",
                outcome="started",
                started_at=old,
            )
            session.add(request_record)
            await session.flush()
            session.add(
                ProviderAttemptRecord(
                    request_id=request_id,
                    attempt_index=1,
                    provider="integration",
                    outcome="started",
                    started_at=old,
                )
            )
            await session.commit()
            record_created = True
        await redis.zadd(policy_key, {request_id: int(time.time() * 1000) - 1})
        await redis.hset(started_key, request_id, int(old.timestamp() * 1000))
        active_started_ms = int(time.time() * 1000) - 10_000
        await redis.zadd(policy_key, {active_request_id: int(time.time() * 1000) + 30_000})
        await redis.hset(started_key, active_request_id, active_started_ms)

        metrics = Metrics("integration")
        await metrics.refresh_operational_state(database, redis)
        samples = {
            sample.name: sample.value
            for metric in metrics.registry.collect()
            for sample in metric.samples
        }
        assert samples["northgate_started_requests"] >= 1
        assert samples["northgate_oldest_started_request_age_seconds"] >= 3500
        assert samples["northgate_active_concurrency_leases"] >= 1
        assert samples["northgate_oldest_active_concurrency_lease_age_seconds"] >= 9

        preview = await reconcile(database, redis, older_than_seconds=900, apply=False)
        assert preview.dry_run is True
        assert preview.stale_requests >= 1
        assert preview.stale_attempts >= 1
        assert preview.expired_leases >= 1
        assert await redis.zscore(policy_key, request_id) is not None

        applied = await reconcile(database, redis, older_than_seconds=900, apply=True)
        assert applied.dry_run is False
        assert applied.released_leases >= 1
        assert await redis.zscore(policy_key, request_id) is None
        assert await redis.hget(started_key, request_id) is None
        assert await redis.zscore(policy_key, active_request_id) is not None

        async with database.sessions() as session:
            request_record = await session.get(RequestRecord, request_id)
            attempt_record = await session.scalar(
                select(ProviderAttemptRecord).where(ProviderAttemptRecord.request_id == request_id)
            )
        assert request_record is not None
        assert request_record.outcome == "settlement_incomplete"
        assert request_record.error_code == "SETTLEMENT_INCOMPLETE"
        assert request_record.total_tokens is None
        assert attempt_record is not None
        assert attempt_record.outcome == "settlement_incomplete"
        assert attempt_record.total_tokens is None

        repeated = await reconcile(database, redis, older_than_seconds=900, apply=True)
        assert repeated.stale_requests == 0
        assert repeated.stale_attempts == 0
        assert repeated.released_leases == 0
    finally:
        if record_created:
            async with database.sessions() as session:
                await session.execute(
                    delete(ProviderAttemptRecord).where(
                        ProviderAttemptRecord.request_id == request_id
                    )
                )
                await session.execute(
                    delete(RequestRecord).where(RequestRecord.request_id == request_id)
                )
                await session.commit()
        try:
            await redis.delete(policy_key)
            await redis.delete(started_key)
        except RedisError:
            pass
        await database.close()
        await redis.aclose()
