import argparse
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import structlog
from redis.asyncio import Redis
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from northgate.config import get_settings
from northgate.db.database import Database
from northgate.db.models import ProviderAttemptRecord, RequestRecord, SettlementEvent
from northgate.policy import PolicyEngine

logger = structlog.get_logger()
WORKER_HEARTBEAT_PATTERN = "northgate:settlement:worker:heartbeat:*"
RECOVERABLE_SETTLEMENT_STATUSES = ("pending", "retry", "processing")
SETTLEMENT_PAYLOAD_SCHEMA_VERSION = 1


class SettlementPayloadError(Exception):
    pass


class SettlementRecordMissingError(Exception):
    pass


class SettlementConflictError(Exception):
    pass


@dataclass(frozen=True)
class SettlementBacklog:
    pending_events: int
    oldest_age_seconds: float


class SettlementCoordinator:
    def __init__(
        self,
        database: Database,
        policy_engine: PolicyEngine | None,
        *,
        max_attempts: int = 20,
        lock_timeout_seconds: int = 60,
    ) -> None:
        self.database = database
        self.policy_engine = policy_engine
        self.max_attempts = max_attempts
        self.lock_timeout_seconds = lock_timeout_seconds

    async def enqueue(
        self,
        *,
        request_id: str,
        payload: dict[str, object],
        event_key: str = "terminal",
    ) -> UUID:
        normalized_payload = dict(payload)
        normalized_payload.setdefault("schema_version", SETTLEMENT_PAYLOAD_SCHEMA_VERSION)
        _validate_payload_schema(normalized_payload)
        policy_settled_at = datetime.now(UTC) if payload.get("policy") is None else None
        async with self.database.sessions() as session:
            existing = await session.scalar(
                select(SettlementEvent).where(
                    SettlementEvent.request_id == request_id,
                    SettlementEvent.event_key == event_key,
                )
            )
            if existing is not None:
                return existing.id
            event = SettlementEvent(
                request_id=request_id,
                event_key=event_key,
                payload=normalized_payload,
                status="pending",
                attempts=0,
                policy_settled_at=policy_settled_at,
            )
            session.add(event)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(SettlementEvent).where(
                        SettlementEvent.request_id == request_id,
                        SettlementEvent.event_key == event_key,
                    )
                )
                if existing is None:
                    raise
                return existing.id
            return event.id

    async def process_one(self) -> bool:
        return await self.process()

    async def process(self, event_id: UUID | None = None) -> bool:
        """Process one event and report whether every settlement stage completed."""

        event = await self._claim(event_id)
        if event is None:
            return False
        try:
            _validate_payload_schema(event.payload)
            if event.database_settled_at is None:
                await self._apply_database(event)
            if event.policy_settled_at is None:
                await self._apply_policy(event)
            await self._complete(event.id)
        except Exception as exc:
            await self._retry(event.id, event.attempts, exc)
            await logger.aexception(
                "settlement_event_failed",
                settlement_event_id=str(event.id),
                request_id=event.request_id,
            )
            return False
        return True

    async def _claim(self, event_id: UUID | None = None) -> SettlementEvent | None:
        now = datetime.now(UTC)
        stale_lock = now - timedelta(seconds=self.lock_timeout_seconds)
        async with self.database.sessions() as session:
            async with session.begin():
                statement = (
                    select(SettlementEvent)
                    .where(
                        or_(
                            SettlementEvent.status.in_(("pending", "retry")),
                            (
                                (SettlementEvent.status == "processing")
                                & (SettlementEvent.locked_at < stale_lock)
                            ),
                        ),
                        SettlementEvent.available_at <= now,
                    )
                    .order_by(SettlementEvent.created_at)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                if event_id is not None:
                    statement = statement.where(SettlementEvent.id == event_id)
                event = await session.scalar(statement)
                if event is None:
                    return None
                event.status = "processing"
                event.locked_at = now
                event.attempts += 1
            return event

    async def _apply_database(self, event: SettlementEvent) -> None:
        request = event.payload.get("request")
        attempt = event.payload.get("attempt")
        now = datetime.now(UTC)
        async with self.database.sessions() as session:
            async with session.begin():
                if attempt is not None:
                    if not isinstance(attempt, dict):
                        raise SettlementPayloadError("attempt must be an object or null")
                    attempt_id = UUID(_string(attempt, "id"))
                    attempt_values = {
                        "outcome": _string(attempt, "outcome"),
                        "http_status": _optional_int(attempt, "status_code"),
                        "provider_request_id": _optional_string(attempt, "provider_request_id"),
                        "prompt_tokens": _optional_int(attempt, "prompt_tokens"),
                        "completion_tokens": _optional_int(attempt, "completion_tokens"),
                        "total_tokens": _optional_int(attempt, "total_tokens"),
                        "cached_prompt_tokens": _optional_int(attempt, "cached_prompt_tokens"),
                        "cost_microusd": _optional_int(attempt, "cost_microusd"),
                        "latency_ms": _optional_int(attempt, "latency_ms"),
                    }
                    result = await session.execute(
                        update(ProviderAttemptRecord)
                        .where(
                            ProviderAttemptRecord.id == attempt_id,
                            ProviderAttemptRecord.outcome == "started",
                        )
                        .values(
                            **attempt_values,
                            completed_at=now,
                        )
                    )
                    if result.rowcount != 1:
                        existing_attempt = await session.get(ProviderAttemptRecord, attempt_id)
                        _assert_terminal_match(
                            existing_attempt,
                            attempt_values,
                            record_name="provider attempt",
                            identifier=str(attempt_id),
                        )
                        if existing_attempt.request_id != event.request_id:
                            raise SettlementConflictError(
                                f"provider attempt {attempt_id} belongs to another request"
                            )
                if request is not None:
                    if not isinstance(request, dict):
                        raise SettlementPayloadError("request must be an object or null")
                    request_values = {
                        "outcome": _string(request, "outcome"),
                        "http_status": _optional_int(request, "status_code"),
                        "provider_request_id": _optional_string(request, "provider_request_id"),
                        "prompt_tokens": _optional_int(request, "prompt_tokens"),
                        "completion_tokens": _optional_int(request, "completion_tokens"),
                        "total_tokens": _optional_int(request, "total_tokens"),
                        "cached_prompt_tokens": _optional_int(request, "cached_prompt_tokens"),
                        "cost_microusd": _optional_int(request, "cost_microusd"),
                        "route_id": _optional_uuid(request, "route_id"),
                        "provider": _string(request, "provider"),
                        "price_id": _optional_uuid(request, "price_id"),
                        "latency_ms": _optional_int(request, "latency_ms"),
                        "first_token_ms": _optional_int(request, "first_token_ms"),
                    }
                    result = await session.execute(
                        update(RequestRecord)
                        .where(
                            RequestRecord.request_id == event.request_id,
                            RequestRecord.outcome == "started",
                        )
                        .values(
                            **request_values,
                            completed_at=now,
                        )
                    )
                    if result.rowcount != 1:
                        existing_request = await session.get(RequestRecord, event.request_id)
                        _assert_terminal_match(
                            existing_request,
                            request_values,
                            record_name="request",
                            identifier=event.request_id,
                        )
                await session.execute(
                    update(SettlementEvent)
                    .where(SettlementEvent.id == event.id)
                    .values(database_settled_at=now)
                )
        event.database_settled_at = now

    async def _apply_policy(self, event: SettlementEvent) -> None:
        policy = _mapping(event.payload, "policy")
        if self.policy_engine is None:
            raise RuntimeError("policy engine is unavailable")
        await self.policy_engine.settle_reservation(
            gateway_key=_string(policy, "gateway_key"),
            request_id=event.request_id,
            token_day=_string(policy, "token_day"),
            spend_day=_string(policy, "spend_day"),
            spend_month=_string(policy, "spend_month"),
            actual_tokens=_optional_int(policy, "actual_tokens"),
            actual_cost_microusd=_optional_int(policy, "actual_cost_microusd"),
        )
        now = datetime.now(UTC)
        async with self.database.sessions() as session:
            await session.execute(
                update(SettlementEvent)
                .where(SettlementEvent.id == event.id)
                .values(policy_settled_at=now)
            )
            await session.commit()
        event.policy_settled_at = now

    async def _complete(self, event_id: UUID) -> None:
        now = datetime.now(UTC)
        async with self.database.sessions() as session:
            await session.execute(
                update(SettlementEvent)
                .where(SettlementEvent.id == event_id)
                .values(
                    status="completed",
                    last_error=None,
                    locked_at=None,
                    completed_at=now,
                )
            )
            await session.commit()

    async def _retry(self, event_id: UUID, attempts: int, exc: Exception) -> None:
        failed = attempts >= self.max_attempts
        delay_seconds = min(60, 2 ** min(attempts, 6))
        message = f"{type(exc).__name__}: {exc}"[:500]
        async with self.database.sessions() as session:
            await session.execute(
                update(SettlementEvent)
                .where(SettlementEvent.id == event_id)
                .values(
                    status="failed" if failed else "retry",
                    last_error=message,
                    locked_at=None,
                    available_at=datetime.now(UTC) + timedelta(seconds=delay_seconds),
                )
            )
            await session.commit()


async def run_worker(coordinator: SettlementCoordinator, poll_seconds: float) -> None:
    while True:
        processed = await coordinator.process_one()
        if not processed:
            await asyncio.sleep(poll_seconds)


async def settlement_worker_available(redis: Redis) -> bool:
    async for _key in redis.scan_iter(match=WORKER_HEARTBEAT_PATTERN, count=10):
        return True
    return False


async def settlement_backlog(database: Database) -> SettlementBacklog:
    async with database.sessions() as session:
        count, oldest = (
            await session.execute(
                select(func.count(), func.min(SettlementEvent.created_at)).where(
                    SettlementEvent.status.in_(RECOVERABLE_SETTLEMENT_STATUSES)
                )
            )
        ).one()
    age = max(0.0, (datetime.now(UTC) - oldest).total_seconds()) if oldest is not None else 0.0
    return SettlementBacklog(pending_events=int(count), oldest_age_seconds=age)


async def cleanup_completed_events(
    database: Database,
    *,
    retention_days: int,
    batch_size: int,
) -> int:
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    candidate_ids = (
        select(SettlementEvent.id)
        .where(
            SettlementEvent.status == "completed",
            SettlementEvent.completed_at < cutoff,
        )
        .order_by(SettlementEvent.completed_at)
        .limit(batch_size)
    )
    async with database.sessions() as session:
        result = await session.execute(
            delete(SettlementEvent).where(SettlementEvent.id.in_(candidate_ids))
        )
        await session.commit()
    return max(0, result.rowcount)


async def _maintain_worker_heartbeat(redis: Redis, ttl_seconds: int) -> None:
    key = f"northgate:settlement:worker:heartbeat:{uuid4()}"
    interval = max(1.0, ttl_seconds / 3)
    try:
        while True:
            await redis.set(key, int(datetime.now(UTC).timestamp() * 1000), ex=ttl_seconds)
            await asyncio.sleep(interval)
    finally:
        try:
            await redis.delete(key)
        except Exception:
            pass


def _mapping(payload: dict[str, object], key: str) -> dict[str, object]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise SettlementPayloadError(f"{key} must be an object")
    return value


def _string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise SettlementPayloadError(f"{key} must be a string")
    return value


def _optional_string(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is not None and not isinstance(value, str):
        raise SettlementPayloadError(f"{key} must be a string or null")
    return value


def _optional_int(payload: dict[str, object], key: str) -> int | None:
    value = payload.get(key)
    if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
        raise SettlementPayloadError(f"{key} must be an integer or null")
    return value


def _optional_uuid(payload: dict[str, object], key: str) -> UUID | None:
    value = _optional_string(payload, key)
    return UUID(value) if value is not None else None


def _validate_payload_schema(payload: dict[str, object]) -> None:
    version = payload.get("schema_version", SETTLEMENT_PAYLOAD_SCHEMA_VERSION)
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != SETTLEMENT_PAYLOAD_SCHEMA_VERSION
    ):
        raise SettlementPayloadError(f"unsupported settlement payload schema version: {version}")


def _assert_terminal_match(
    record: ProviderAttemptRecord | RequestRecord | None,
    expected: dict[str, object],
    *,
    record_name: str,
    identifier: str,
) -> None:
    if record is None:
        raise SettlementRecordMissingError(f"{record_name} {identifier} does not exist")
    mismatched = [field for field, value in expected.items() if getattr(record, field) != value]
    if record.completed_at is None:
        mismatched.append("completed_at")
    if mismatched:
        fields = ", ".join(sorted(mismatched))
        raise SettlementConflictError(
            f"{record_name} {identifier} conflicts with settlement fields: {fields}"
        )


async def _run_cli(*, once: bool, poll_seconds: float) -> None:
    settings = get_settings()
    database = Database(settings.database_url.get_secret_value())
    redis = Redis.from_url(settings.redis_url.get_secret_value())
    policy_engine = PolicyEngine(redis, lease_seconds=settings.concurrency_lease_seconds)
    coordinator = SettlementCoordinator(database, policy_engine)
    try:
        if once:
            while await coordinator.process_one():
                pass
        else:
            heartbeat = asyncio.create_task(
                _maintain_worker_heartbeat(
                    redis,
                    settings.settlement_worker_heartbeat_ttl_seconds,
                )
            )
            try:
                await run_worker(coordinator, poll_seconds)
            finally:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass
    finally:
        await database.close()
        await redis.aclose()


async def _run_healthcheck() -> None:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url.get_secret_value())
    try:
        if not await settlement_worker_available(redis):
            raise SystemExit("no live settlement worker heartbeat")
    finally:
        await redis.aclose()


async def _run_cleanup(*, retention_days: int, batch_size: int) -> None:
    settings = get_settings()
    database = Database(settings.database_url.get_secret_value())
    try:
        deleted = await cleanup_completed_events(
            database,
            retention_days=retention_days,
            batch_size=batch_size,
        )
        print(f"deleted_events={deleted}")
    finally:
        await database.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Process durable Northgate settlement events.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Drain available events and exit.")
    mode.add_argument(
        "--healthcheck",
        action="store_true",
        help="Exit successfully when a live worker heartbeat is visible.",
    )
    mode.add_argument(
        "--cleanup-completed",
        action="store_true",
        help="Delete one batch of completed events older than the retention period.",
    )
    parser.add_argument("--poll-seconds", type=float, default=0.25)
    parser.add_argument("--retention-days", type=int)
    parser.add_argument("--cleanup-batch-size", type=int, default=1000)
    args = parser.parse_args()
    if not 0.05 <= args.poll_seconds <= 60:
        raise SystemExit("--poll-seconds must be between 0.05 and 60")
    if args.healthcheck:
        asyncio.run(_run_healthcheck())
    elif args.cleanup_completed:
        retention_days = (
            args.retention_days
            if args.retention_days is not None
            else get_settings().settlement_completed_retention_days
        )
        if not 1 <= retention_days <= 3650:
            raise SystemExit("--retention-days must be between 1 and 3650")
        if not 1 <= args.cleanup_batch_size <= 10000:
            raise SystemExit("--cleanup-batch-size must be between 1 and 10000")
        asyncio.run(
            _run_cleanup(
                retention_days=retention_days,
                batch_size=args.cleanup_batch_size,
            )
        )
    else:
        asyncio.run(_run_cli(once=args.once, poll_seconds=args.poll_seconds))


if __name__ == "__main__":
    main()
