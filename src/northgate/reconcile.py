import argparse
import asyncio
import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis
from sqlalchemy import select, update

from northgate.config import get_settings
from northgate.db.database import Database
from northgate.db.models import ProviderAttemptRecord, RequestRecord


@dataclass(frozen=True)
class ReconciliationReport:
    dry_run: bool
    cutoff: str
    stale_requests: int
    stale_attempts: int
    concurrency_keys: int
    expired_leases: int
    released_leases: int


async def reconcile(
    database: Database,
    redis: Redis,
    *,
    older_than_seconds: int,
    apply: bool,
) -> ReconciliationReport:
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=older_than_seconds)
    async with database.sessions() as session:
        request_ids = list(
            (
                await session.scalars(
                    select(RequestRecord.request_id).where(
                        RequestRecord.outcome == "started",
                        RequestRecord.started_at < cutoff,
                    )
                )
            ).all()
        )
        attempt_ids = list(
            (
                await session.scalars(
                    select(ProviderAttemptRecord.id).where(
                        ProviderAttemptRecord.outcome == "started",
                        ProviderAttemptRecord.started_at < cutoff,
                    )
                )
            ).all()
        )

    now_ms = int(time.time() * 1000)
    concurrency_keys = 0
    expired_leases = 0
    released_leases = 0
    async for key in redis.scan_iter(match="northgate:policy:*:concurrency"):
        concurrency_keys += 1
        started_key = key + b":started" if isinstance(key, bytes) else f"{key}:started"
        expired_request_ids = await redis.zrangebyscore(key, "-inf", now_ms)
        expired_leases += len(expired_request_ids)
        if apply:
            if request_ids:
                released_leases += int(await redis.zrem(key, *request_ids))
                await redis.hdel(started_key, *request_ids)
            if expired_request_ids:
                await redis.hdel(started_key, *expired_request_ids)
            released_leases += int(await redis.zremrangebyscore(key, "-inf", now_ms))

    if apply:
        async with database.sessions() as session:
            if attempt_ids:
                await session.execute(
                    update(ProviderAttemptRecord)
                    .where(
                        ProviderAttemptRecord.id.in_(attempt_ids),
                        ProviderAttemptRecord.outcome == "started",
                    )
                    .values(
                        outcome="settlement_incomplete",
                        completed_at=now,
                    )
                )
            if request_ids:
                await session.execute(
                    update(RequestRecord)
                    .where(
                        RequestRecord.request_id.in_(request_ids),
                        RequestRecord.outcome == "started",
                    )
                    .values(
                        outcome="settlement_incomplete",
                        error_code="SETTLEMENT_INCOMPLETE",
                        completed_at=now,
                    )
                )
            await session.commit()

    return ReconciliationReport(
        dry_run=not apply,
        cutoff=cutoff.isoformat(),
        stale_requests=len(request_ids),
        stale_attempts=len(attempt_ids),
        concurrency_keys=concurrency_keys,
        expired_leases=expired_leases,
        released_leases=released_leases,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reconcile stale Northgate records and expired concurrency leases."
    )
    parser.add_argument(
        "--older-than-seconds",
        type=int,
        default=900,
        help="Mark started records older than this threshold (default: 900).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply recovery. Without this flag the command only reports candidates.",
    )
    return parser


async def _run(older_than_seconds: int, apply: bool) -> ReconciliationReport:
    settings = get_settings()
    database = Database(settings.database_url.get_secret_value())
    redis = Redis.from_url(settings.redis_url.get_secret_value())
    try:
        return await reconcile(
            database,
            redis,
            older_than_seconds=older_than_seconds,
            apply=apply,
        )
    finally:
        await database.close()
        await redis.aclose()


def main() -> None:
    args = _parser().parse_args()
    if args.older_than_seconds < 30:
        raise SystemExit("--older-than-seconds must be at least 30")
    report = asyncio.run(_run(args.older_than_seconds, args.apply))
    print(json.dumps(asdict(report), sort_keys=True))


if __name__ == "__main__":
    main()
