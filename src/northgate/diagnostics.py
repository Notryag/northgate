import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from fastapi import Query, Request
from fastapi.responses import JSONResponse, Response
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import and_, or_, select

from northgate.db.database import Database
from northgate.db.models import ProviderAttemptRecord, RequestRecord, SettlementEvent
from northgate.operator_auth import authorize_operator

DIAGNOSTICS_SCHEMA_VERSION = 1
_MAX_RANGE = timedelta(days=90)
_METADATA_KEY = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_REQUEST_ID = re.compile(r"^req_[A-Za-z0-9_-]{8,120}$")
_RECOVERABLE_SETTLEMENT_STATUSES = ("pending", "retry", "processing")
_MAX_POLICY_KEYS = 1000


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _finding(
    code: str,
    severity: str,
    request_id: str,
    evidence: dict[str, object] | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "code": code,
        "severity": severity,
        "request_id": request_id,
    }
    if evidence:
        result["evidence"] = evidence
    return result


def _request_fields(record: RequestRecord) -> dict[str, object]:
    reserved_total = getattr(record, "reserved_total_tokens", None)
    if reserved_total is None:
        reserved_total = record.estimated_tokens
    actual_total = record.total_tokens
    return {
        "request_id": record.request_id,
        "model": record.model,
        "provider": record.provider,
        "outcome": record.outcome,
        "http_status": record.http_status,
        "error_code": record.error_code,
        "estimated_tokens": record.estimated_tokens,
        "estimated_prompt_tokens": getattr(record, "estimated_prompt_tokens", None),
        "reserved_output_tokens": getattr(record, "reserved_output_tokens", None),
        "attempt_multiplier": getattr(record, "attempt_multiplier", None),
        "reservation_margin_tokens": getattr(record, "reservation_margin_tokens", None),
        "reserved_total_tokens": reserved_total,
        "actual_total_tokens": actual_total,
        "released_tokens": (
            max(0, reserved_total - actual_total)
            if reserved_total is not None and actual_total is not None
            else None
        ),
        "estimate_actual_ratio": (
            round(reserved_total / actual_total, 4)
            if reserved_total is not None and actual_total is not None and actual_total > 0
            else None
        ),
        "token_estimator": getattr(record, "token_estimator", None),
        "output_limit_source": getattr(record, "output_limit_source", None),
        "prompt_tokens": record.prompt_tokens,
        "completion_tokens": record.completion_tokens,
        "total_tokens": record.total_tokens,
        "cached_prompt_tokens": record.cached_prompt_tokens,
        "cost_microusd": record.cost_microusd,
        "cache_status": record.cache_status,
        "metadata_trust": record.request_metadata_trust or {},
        "latency_ms": record.latency_ms,
        "first_token_ms": record.first_token_ms,
        "started_at": record.started_at.isoformat(),
        "completed_at": _iso(record.completed_at),
    }


def _attempt_fields(attempt: ProviderAttemptRecord) -> dict[str, object]:
    return {
        "attempt_id": str(attempt.id),
        "attempt_index": attempt.attempt_index,
        "route_id": str(attempt.route_id) if attempt.route_id is not None else None,
        "provider": attempt.provider,
        "outcome": attempt.outcome,
        "http_status": attempt.http_status,
        "provider_request_id": attempt.provider_request_id,
        "prompt_tokens": attempt.prompt_tokens,
        "completion_tokens": attempt.completion_tokens,
        "total_tokens": attempt.total_tokens,
        "cached_prompt_tokens": attempt.cached_prompt_tokens,
        "cost_microusd": attempt.cost_microusd,
        "latency_ms": attempt.latency_ms,
        "started_at": attempt.started_at.isoformat(),
        "completed_at": _iso(attempt.completed_at),
    }


def _settlement_fields(event: SettlementEvent) -> dict[str, object]:
    schema_version = event.payload.get("schema_version")
    return {
        "event_id": str(event.id),
        "event_key": event.event_key,
        "schema_version": schema_version if isinstance(schema_version, int) else None,
        "status": event.status,
        "attempts": event.attempts,
        "database_settled_at": _iso(event.database_settled_at),
        "policy_settled_at": _iso(event.policy_settled_at),
        "created_at": event.created_at.isoformat(),
        "completed_at": _iso(event.completed_at),
    }


def build_request_diagnostic(
    record: RequestRecord,
    attempts: Sequence[ProviderAttemptRecord],
    events: Sequence[SettlementEvent],
    *,
    settlement_expected: bool,
) -> dict[str, object]:
    ordered_attempts = sorted(attempts, key=lambda item: item.attempt_index)
    ordered_events = sorted(events, key=lambda item: (item.created_at, item.event_key))
    findings: list[dict[str, object]] = []
    request_id = record.request_id

    if record.outcome == "started":
        findings.append(_finding("REQUEST_STILL_STARTED", "error", request_id))
    started_attempts = [
        item.attempt_index for item in ordered_attempts if item.outcome == "started"
    ]
    if started_attempts:
        findings.append(
            _finding(
                "ATTEMPT_STILL_STARTED",
                "error",
                request_id,
                {"attempt_indexes": started_attempts},
            )
        )

    terminal_event = next((item for item in ordered_events if item.event_key == "terminal"), None)
    terminal_http = record.http_status is not None or any(
        item.http_status is not None for item in ordered_attempts
    )
    if settlement_expected and terminal_http and terminal_event is None:
        findings.append(_finding("TERMINAL_HTTP_WITHOUT_SETTLEMENT", "error", request_id))
    if record.total_tokens is None:
        findings.append(_finding("USAGE_MISSING", "warning", request_id))
    elif record.prompt_tokens is not None and record.cached_prompt_tokens is None:
        findings.append(_finding("CACHED_USAGE_MISSING", "info", request_id))
    elif (
        record.prompt_tokens is not None
        and record.prompt_tokens > 0
        and record.cached_prompt_tokens == 0
    ):
        findings.append(_finding("PROMPT_CACHE_NOT_HIT", "info", request_id))
    if record.cache_status == "bypass":
        findings.append(_finding("EXACT_CACHE_BYPASSED", "info", request_id))

    metadata = record.request_metadata or {}
    metadata_trust = record.request_metadata_trust or {}
    missing_trust = sorted(key for key in metadata if not metadata_trust.get(key))
    if missing_trust:
        findings.append(
            _finding(
                "METADATA_TRUST_MISSING",
                "warning",
                request_id,
                {"metadata_keys": missing_trust},
            )
        )
    if len(ordered_attempts) > 1:
        findings.append(
            _finding(
                "RETRY_OR_FALLBACK_USED",
                "info",
                request_id,
                {"attempt_count": len(ordered_attempts)},
            )
        )

    attempt_totals = [item.total_tokens for item in ordered_attempts]
    if (
        record.total_tokens is not None
        and attempt_totals
        and all(value is not None for value in attempt_totals)
    ):
        attempt_total = sum(value for value in attempt_totals if value is not None)
        if attempt_total != record.total_tokens:
            findings.append(
                _finding(
                    "REQUEST_ATTEMPT_TOTAL_MISMATCH",
                    "error",
                    request_id,
                    {
                        "request_total_tokens": record.total_tokens,
                        "attempt_total_tokens": attempt_total,
                    },
                )
            )

    return {
        "schema_version": DIAGNOSTICS_SCHEMA_VERSION,
        "request": _request_fields(record),
        "attempts": [_attempt_fields(item) for item in ordered_attempts],
        "settlement": {
            "expected": settlement_expected,
            "events": [_settlement_fields(item) for item in ordered_events],
        },
        "findings": findings,
    }


def build_correlated_diagnostic(
    diagnostics: Sequence[dict[str, object]],
    *,
    metadata_key: str,
    metadata_value: str,
    start: datetime,
    end: datetime,
    has_more: bool,
) -> dict[str, object]:
    requests = [item["request"] for item in diagnostics]
    findings = [finding for item in diagnostics for finding in item["findings"]]

    def total(field: str) -> int:
        return sum(
            value
            for request in requests
            if isinstance(request, dict) and isinstance((value := request.get(field)), int)
        )

    prompt_tokens = total("prompt_tokens")
    cached_prompt_tokens = total("cached_prompt_tokens")
    cached_usage_missing_requests = sum(
        1
        for request in requests
        if isinstance(request, dict)
        and request.get("prompt_tokens") is not None
        and request.get("cached_prompt_tokens") is None
    )
    finding_counts = Counter(
        finding["code"]
        for finding in findings
        if isinstance(finding, dict) and isinstance(finding.get("code"), str)
    )
    return {
        "schema_version": DIAGNOSTICS_SCHEMA_VERSION,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "correlation": {"metadata_key": metadata_key, "metadata_value": metadata_value},
        "has_more": has_more,
        "aggregate": {
            "requests": len(requests),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": total("completion_tokens"),
            "total_tokens": total("total_tokens"),
            "cached_prompt_tokens": cached_prompt_tokens,
            "cost_microusd": total("cost_microusd"),
            "usage_missing_requests": sum(
                1
                for request in requests
                if isinstance(request, dict) and request.get("total_tokens") is None
            ),
            "prompt_cache_percent": (
                round(cached_prompt_tokens * 100 / prompt_tokens, 2) if prompt_tokens else None
            ),
            "cached_usage_missing_requests": cached_usage_missing_requests,
            "prompt_cache_percent_is_lower_bound": cached_usage_missing_requests > 0,
            "retry_fallback_requests": sum(
                1
                for item in diagnostics
                if isinstance(item.get("attempts"), list) and len(item["attempts"]) > 1
            ),
        },
        "finding_counts": dict(sorted(finding_counts.items())),
        "requests": list(diagnostics),
        "findings": findings,
    }


def build_usage_diagnostic(
    diagnostics: Sequence[dict[str, object]],
    *,
    metadata_key: str,
    metadata_value: str,
    group_by: str | None,
    group_values: Sequence[tuple[str | None, str | None]],
    filter_trust_values: Sequence[str | None],
    start: datetime,
    end: datetime,
    has_more: bool,
    excessive_ratio_threshold: float = 3.0,
    excessive_min_sample_size: int = 10,
) -> dict[str, object]:
    result = build_correlated_diagnostic(
        diagnostics,
        metadata_key=metadata_key,
        metadata_value=metadata_value,
        start=start,
        end=end,
        has_more=has_more,
    )
    result["filter"] = result.pop("correlation")
    result["filter"]["metadata_trust"] = sorted(
        {value for value in filter_trust_values if value is not None}
    )
    result["group_by"] = group_by
    groups: list[dict[str, object]] = []
    if group_by is not None:
        grouped: dict[str | None, list[tuple[dict[str, object], str | None]]] = defaultdict(list)
        for diagnostic, (value, trust) in zip(diagnostics, group_values, strict=True):
            grouped[value].append((diagnostic, trust))
        for value, items in grouped.items():
            group_diagnostics = [item[0] for item in items]
            aggregate = build_correlated_diagnostic(
                group_diagnostics,
                metadata_key=metadata_key,
                metadata_value=metadata_value,
                start=start,
                end=end,
                has_more=False,
            )
            groups.append(
                {
                    "metadata_value": value,
                    "metadata_trust": sorted({trust for _, trust in items if trust is not None}),
                    "aggregate": aggregate["aggregate"],
                    "finding_counts": aggregate["finding_counts"],
                    "latest_started_at": max(
                        str(item["request"]["started_at"])
                        for item in group_diagnostics
                        if isinstance(item.get("request"), dict)
                    ),
                }
            )
    groups.sort(key=lambda item: str(item["latest_started_at"]), reverse=True)
    result["groups"] = groups
    requests = [item.get("request") for item in diagnostics]
    ratio_sample = [
        item
        for item in requests
        if isinstance(item, dict)
        and isinstance(item.get("reserved_total_tokens"), int)
        and isinstance(item.get("actual_total_tokens"), int)
        and item["actual_total_tokens"] > 0
    ]
    reserved_total = sum(int(item["reserved_total_tokens"]) for item in ratio_sample)
    actual_total = sum(int(item["actual_total_tokens"]) for item in ratio_sample)
    aggregate_ratio = round(reserved_total / actual_total, 4) if actual_total else None
    aggregate = result.get("aggregate")
    if isinstance(aggregate, dict):
        aggregate["reservation_sample_requests"] = len(ratio_sample)
        aggregate["reserved_total_tokens"] = reserved_total
        aggregate["actual_total_tokens"] = actual_total
        aggregate["released_tokens"] = max(0, reserved_total - actual_total)
        aggregate["estimate_actual_ratio"] = aggregate_ratio
    if (
        len(ratio_sample) >= excessive_min_sample_size
        and aggregate_ratio is not None
        and aggregate_ratio >= excessive_ratio_threshold
    ):
        finding = _finding(
            "EXCESSIVE_TOKEN_RESERVATION",
            "warning",
            "aggregate",
            {
                "sample_requests": len(ratio_sample),
                "reserved_total_tokens": reserved_total,
                "actual_total_tokens": actual_total,
                "estimate_actual_ratio": aggregate_ratio,
                "threshold": excessive_ratio_threshold,
            },
        )
        findings = result.get("findings")
        if isinstance(findings, list):
            findings.append(finding)
        finding_counts = result.get("finding_counts")
        if isinstance(finding_counts, dict):
            finding_counts["EXCESSIVE_TOKEN_RESERVATION"] = 1
    return result


class DiagnosticsService:
    def __init__(
        self,
        database: Database,
        *,
        settlement_expected: bool,
        excessive_ratio_threshold: float = 3.0,
        excessive_min_sample_size: int = 10,
    ) -> None:
        self.database = database
        self.settlement_expected = settlement_expected
        self.excessive_ratio_threshold = excessive_ratio_threshold
        self.excessive_min_sample_size = excessive_min_sample_size

    async def inspect_request(self, request_id: str) -> dict[str, object] | None:
        async with self.database.sessions() as session:
            record = await session.get(RequestRecord, request_id)
            if record is None:
                return None
            attempts = (
                await session.scalars(
                    select(ProviderAttemptRecord)
                    .where(ProviderAttemptRecord.request_id == request_id)
                    .order_by(ProviderAttemptRecord.attempt_index)
                )
            ).all()
            events = (
                await session.scalars(
                    select(SettlementEvent)
                    .where(SettlementEvent.request_id == request_id)
                    .order_by(SettlementEvent.created_at, SettlementEvent.event_key)
                )
            ).all()
        return build_request_diagnostic(
            record,
            attempts,
            events,
            settlement_expected=self.settlement_expected,
        )

    async def inspect_correlated(
        self,
        *,
        metadata_key: str,
        metadata_value: str,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> dict[str, object]:
        async with self.database.sessions() as session:
            records = list(
                (
                    await session.scalars(
                        select(RequestRecord)
                        .where(
                            RequestRecord.started_at >= start,
                            RequestRecord.started_at < end,
                            RequestRecord.request_metadata[metadata_key].as_string()
                            == metadata_value,
                        )
                        .order_by(RequestRecord.started_at, RequestRecord.request_id)
                        .limit(limit + 1)
                    )
                ).all()
            )
            has_more = len(records) > limit
            records = records[:limit]
            request_ids = [record.request_id for record in records]
            attempts: Sequence[ProviderAttemptRecord] = ()
            events: Sequence[SettlementEvent] = ()
            if request_ids:
                attempts = (
                    await session.scalars(
                        select(ProviderAttemptRecord).where(
                            ProviderAttemptRecord.request_id.in_(request_ids)
                        )
                    )
                ).all()
                events = (
                    await session.scalars(
                        select(SettlementEvent).where(SettlementEvent.request_id.in_(request_ids))
                    )
                ).all()

        attempts_by_request: dict[str, list[ProviderAttemptRecord]] = defaultdict(list)
        for attempt in attempts:
            attempts_by_request[attempt.request_id].append(attempt)
        events_by_request: dict[str, list[SettlementEvent]] = defaultdict(list)
        for event in events:
            events_by_request[event.request_id].append(event)
        diagnostics = [
            build_request_diagnostic(
                record,
                attempts_by_request[record.request_id],
                events_by_request[record.request_id],
                settlement_expected=self.settlement_expected,
            )
            for record in records
        ]
        return build_correlated_diagnostic(
            diagnostics,
            metadata_key=metadata_key,
            metadata_value=metadata_value,
            start=start,
            end=end,
            has_more=has_more,
        )

    async def inspect_usage(
        self,
        *,
        metadata_key: str,
        metadata_value: str,
        group_by: str | None,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> dict[str, object]:
        async with self.database.sessions() as session:
            records = list(
                (
                    await session.scalars(
                        select(RequestRecord)
                        .where(
                            RequestRecord.started_at >= start,
                            RequestRecord.started_at < end,
                            RequestRecord.request_metadata[metadata_key].as_string()
                            == metadata_value,
                        )
                        .order_by(RequestRecord.started_at.desc(), RequestRecord.request_id.desc())
                        .limit(limit + 1)
                    )
                ).all()
            )
            has_more = len(records) > limit
            records = records[:limit]
            request_ids = [record.request_id for record in records]
            attempts: Sequence[ProviderAttemptRecord] = ()
            events: Sequence[SettlementEvent] = ()
            if request_ids:
                attempts = (
                    await session.scalars(
                        select(ProviderAttemptRecord).where(
                            ProviderAttemptRecord.request_id.in_(request_ids)
                        )
                    )
                ).all()
                events = (
                    await session.scalars(
                        select(SettlementEvent).where(SettlementEvent.request_id.in_(request_ids))
                    )
                ).all()

        attempts_by_request: dict[str, list[ProviderAttemptRecord]] = defaultdict(list)
        for attempt in attempts:
            attempts_by_request[attempt.request_id].append(attempt)
        events_by_request: dict[str, list[SettlementEvent]] = defaultdict(list)
        for event in events:
            events_by_request[event.request_id].append(event)
        diagnostics = [
            build_request_diagnostic(
                record,
                attempts_by_request[record.request_id],
                events_by_request[record.request_id],
                settlement_expected=self.settlement_expected,
            )
            for record in records
        ]
        group_values = [
            (
                (record.request_metadata or {}).get(group_by) if group_by is not None else None,
                (record.request_metadata_trust or {}).get(group_by)
                if group_by is not None
                else None,
            )
            for record in records
        ]
        filter_trust_values = [
            (record.request_metadata_trust or {}).get(metadata_key) for record in records
        ]
        return build_usage_diagnostic(
            diagnostics,
            metadata_key=metadata_key,
            metadata_value=metadata_value,
            group_by=group_by,
            group_values=group_values,
            filter_trust_values=filter_trust_values,
            start=start,
            end=end,
            has_more=has_more,
            excessive_ratio_threshold=self.excessive_ratio_threshold,
            excessive_min_sample_size=self.excessive_min_sample_size,
        )

    async def inspect_stale(
        self,
        *,
        redis: Redis | None,
        minimum_age_seconds: int,
        limit: int,
    ) -> dict[str, object]:
        now = datetime.now(UTC)
        cutoff = now - timedelta(seconds=minimum_age_seconds)
        stale_attempt_requests = select(ProviderAttemptRecord.request_id).where(
            ProviderAttemptRecord.outcome == "started",
            ProviderAttemptRecord.started_at < cutoff,
        )
        async with self.database.sessions() as session:
            records = list(
                (
                    await session.scalars(
                        select(RequestRecord)
                        .where(
                            or_(
                                and_(
                                    RequestRecord.outcome == "started",
                                    RequestRecord.started_at < cutoff,
                                ),
                                RequestRecord.request_id.in_(stale_attempt_requests),
                            )
                        )
                        .order_by(RequestRecord.started_at, RequestRecord.request_id)
                        .limit(limit + 1)
                    )
                ).all()
            )
            has_more = len(records) > limit
            records = records[:limit]
            request_ids = [record.request_id for record in records]
            attempts: Sequence[ProviderAttemptRecord] = ()
            events: Sequence[SettlementEvent] = ()
            if request_ids:
                attempts = (
                    await session.scalars(
                        select(ProviderAttemptRecord).where(
                            ProviderAttemptRecord.request_id.in_(request_ids)
                        )
                    )
                ).all()
                events = (
                    await session.scalars(
                        select(SettlementEvent).where(SettlementEvent.request_id.in_(request_ids))
                    )
                ).all()

        attempts_by_request: dict[str, list[ProviderAttemptRecord]] = defaultdict(list)
        for attempt in attempts:
            attempts_by_request[attempt.request_id].append(attempt)
        events_by_request: dict[str, list[SettlementEvent]] = defaultdict(list)
        for event in events:
            events_by_request[event.request_id].append(event)
        leases_by_request, policy_keys_truncated = await _policy_leases(
            redis,
            request_ids,
            now=now,
        )

        diagnostics: list[dict[str, object]] = []
        all_findings: list[dict[str, object]] = []
        for record in records:
            request_attempts = attempts_by_request[record.request_id]
            request_events = events_by_request[record.request_id]
            diagnostic = build_request_diagnostic(
                record,
                request_attempts,
                request_events,
                settlement_expected=self.settlement_expected,
            )
            stale_attempt_indexes = [
                attempt.attempt_index
                for attempt in request_attempts
                if attempt.outcome == "started" and attempt.started_at < cutoff
            ]
            recoverable = any(
                event.status in _RECOVERABLE_SETTLEMENT_STATUSES for event in request_events
            )
            stale_findings = [
                _finding(
                    "RECOVERABLE_SETTLEMENT_PENDING"
                    if recoverable
                    else "UNPROTECTED_STALE_SETTLEMENT",
                    "info" if recoverable else "error",
                    record.request_id,
                )
            ]
            request_leases = leases_by_request.get(record.request_id, [])
            if request_leases:
                stale_findings.append(
                    _finding(
                        "STALE_CONCURRENCY_LEASE",
                        "error" if any(item["expired"] for item in request_leases) else "warning",
                        record.request_id,
                        {"lease_count": len(request_leases)},
                    )
                )
            findings = diagnostic["findings"]
            if isinstance(findings, list):
                findings.extend(stale_findings)
                all_findings.extend(findings)
            diagnostic["stale"] = {
                "request_started": record.outcome == "started" and record.started_at < cutoff,
                "stale_attempt_indexes": stale_attempt_indexes,
                "age_seconds": max(0, round((now - record.started_at).total_seconds())),
                "recoverable_settlement": recoverable,
                "concurrency_leases": request_leases,
            }
            diagnostics.append(diagnostic)

        finding_counts = Counter(item["code"] for item in all_findings)
        return {
            "schema_version": DIAGNOSTICS_SCHEMA_VERSION,
            "cutoff": cutoff.isoformat(),
            "minimum_age_seconds": minimum_age_seconds,
            "has_more": has_more,
            "policy_state_available": redis is not None,
            "policy_keys_truncated": policy_keys_truncated,
            "finding_counts": dict(sorted(finding_counts.items())),
            "requests": diagnostics,
            "findings": all_findings,
        }


async def _policy_leases(
    redis: Redis | None,
    request_ids: Sequence[str],
    *,
    now: datetime,
) -> tuple[dict[str, list[dict[str, object]]], bool]:
    leases: dict[str, list[dict[str, object]]] = defaultdict(list)
    if redis is None or not request_ids:
        return leases, False
    now_ms = int(now.timestamp() * 1000)
    keys_seen = 0
    truncated = False
    async for raw_key in redis.scan_iter(match="northgate:policy:*:concurrency"):
        keys_seen += 1
        if keys_seen > _MAX_POLICY_KEYS:
            truncated = True
            break
        key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
        scores = await redis.zmscore(key, request_ids)
        started_values = await redis.hmget(f"{key}:started", request_ids)
        for request_id, score, started_value in zip(
            request_ids,
            scores,
            started_values,
            strict=True,
        ):
            if score is None:
                continue
            started_ms = int(started_value) if started_value is not None else None
            leases[request_id].append(
                {
                    "policy_key": key,
                    "started_at": (
                        datetime.fromtimestamp(started_ms / 1000, UTC).isoformat()
                        if started_ms is not None
                        else None
                    ),
                    "expires_at": datetime.fromtimestamp(score / 1000, UTC).isoformat(),
                    "age_seconds": (
                        max(0, round((now_ms - started_ms) / 1000))
                        if started_ms is not None
                        else None
                    ),
                    "expired": score <= now_ms,
                }
            )
    return leases, truncated


def _database(request: Request) -> Database | None:
    return request.app.state.database


def _service(request: Request, database: Database) -> DiagnosticsService:
    settings = request.app.state.settings
    return DiagnosticsService(
        database,
        settlement_expected=settings.settlement_outbox_enabled,
        excessive_ratio_threshold=settings.policy_estimate_excess_ratio_threshold,
        excessive_min_sample_size=settings.policy_estimate_excess_min_sample_size,
    )


def _range(start: datetime | None, end: datetime | None) -> tuple[datetime, datetime] | None:
    resolved_end = end or datetime.now(UTC)
    resolved_start = start or resolved_end - timedelta(hours=24)
    if resolved_start.tzinfo is None or resolved_end.tzinfo is None:
        return None
    if resolved_start >= resolved_end or resolved_end - resolved_start > _MAX_RANGE:
        return None
    return resolved_start, resolved_end


async def diagnostics_request(request: Request, request_id: str) -> Response:
    authorization_error = authorize_operator(request)
    if authorization_error is not None:
        return authorization_error
    if not _REQUEST_ID.fullmatch(request_id):
        return JSONResponse(
            {"error": {"code": "INVALID_REQUEST_ID", "message": "Invalid request ID"}},
            status_code=400,
        )
    database = _database(request)
    if database is None:
        return JSONResponse(
            {"error": {"code": "DIAGNOSTICS_UNAVAILABLE", "message": "Diagnostics unavailable"}},
            status_code=503,
        )
    result = await _service(request, database).inspect_request(request_id)
    if result is None:
        return JSONResponse(
            {"error": {"code": "REQUEST_NOT_FOUND", "message": "Request not found"}},
            status_code=404,
        )
    return JSONResponse(result)


async def diagnostics_correlated(
    request: Request,
    metadata_key: str,
    metadata_value: str,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> Response:
    authorization_error = authorize_operator(request)
    if authorization_error is not None:
        return authorization_error
    if not _METADATA_KEY.fullmatch(metadata_key) or not 1 <= len(metadata_value) <= 256:
        return JSONResponse(
            {"error": {"code": "INVALID_METADATA_FILTER", "message": "Invalid metadata filter"}},
            status_code=400,
        )
    selected_range = _range(start, end)
    if selected_range is None:
        return JSONResponse(
            {"error": {"code": "INVALID_TIME_RANGE", "message": "Invalid time range"}},
            status_code=400,
        )
    database = _database(request)
    if database is None:
        return JSONResponse(
            {"error": {"code": "DIAGNOSTICS_UNAVAILABLE", "message": "Diagnostics unavailable"}},
            status_code=503,
        )
    resolved_start, resolved_end = selected_range
    result = await _service(request, database).inspect_correlated(
        metadata_key=metadata_key,
        metadata_value=metadata_value,
        start=resolved_start,
        end=resolved_end,
        limit=limit,
    )
    return JSONResponse(result)


async def diagnostics_usage(
    request: Request,
    metadata_key: str,
    metadata_value: str,
    group_by: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=100),
) -> Response:
    authorization_error = authorize_operator(request)
    if authorization_error is not None:
        return authorization_error
    if (
        not _METADATA_KEY.fullmatch(metadata_key)
        or not 1 <= len(metadata_value) <= 256
        or (group_by is not None and not _METADATA_KEY.fullmatch(group_by))
    ):
        return JSONResponse(
            {"error": {"code": "INVALID_METADATA_FILTER", "message": "Invalid metadata filter"}},
            status_code=400,
        )
    selected_range = _range(start, end)
    if selected_range is None:
        return JSONResponse(
            {"error": {"code": "INVALID_TIME_RANGE", "message": "Invalid time range"}},
            status_code=400,
        )
    database = _database(request)
    if database is None:
        return JSONResponse(
            {"error": {"code": "DIAGNOSTICS_UNAVAILABLE", "message": "Diagnostics unavailable"}},
            status_code=503,
        )
    resolved_start, resolved_end = selected_range
    result = await _service(request, database).inspect_usage(
        metadata_key=metadata_key,
        metadata_value=metadata_value,
        group_by=group_by,
        start=resolved_start,
        end=resolved_end,
        limit=limit,
    )
    return JSONResponse(result)


async def diagnostics_capabilities(request: Request) -> Response:
    authorization_error = authorize_operator(request)
    if authorization_error is not None:
        return authorization_error
    return JSONResponse(
        {
            "schema_version": DIAGNOSTICS_SCHEMA_VERSION,
            "service": "northgate-diagnostics",
            "capabilities": ["request", "correlated", "usage", "stale"],
        }
    )


async def diagnostics_stale(
    request: Request,
    minimum_age_seconds: int = Query(default=300, ge=30, le=86400),
    limit: int = Query(default=100, ge=1, le=100),
) -> Response:
    authorization_error = authorize_operator(request)
    if authorization_error is not None:
        return authorization_error
    database = _database(request)
    if database is None:
        return JSONResponse(
            {"error": {"code": "DIAGNOSTICS_UNAVAILABLE", "message": "Diagnostics unavailable"}},
            status_code=503,
        )
    try:
        result = await _service(request, database).inspect_stale(
            redis=request.app.state.redis,
            minimum_age_seconds=minimum_age_seconds,
            limit=limit,
        )
    except RedisError:
        return JSONResponse(
            {
                "error": {
                    "code": "DIAGNOSTICS_POLICY_UNAVAILABLE",
                    "message": "Diagnostics policy state unavailable",
                }
            },
            status_code=503,
        )
    return JSONResponse(result)
