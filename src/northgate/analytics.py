import re
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from fastapi import Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import case, func, select

from northgate.db.database import Database
from northgate.db.models import ProviderAttemptRecord, RequestRecord, Route
from northgate.operator_auth import authorize_operator

_MAX_RANGE = timedelta(days=90)
_METADATA_KEY = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _range(start: datetime | None, end: datetime | None) -> tuple[datetime, datetime] | None:
    resolved_end = end or datetime.now(UTC)
    resolved_start = start or resolved_end - timedelta(hours=24)
    if resolved_start.tzinfo is None or resolved_end.tzinfo is None:
        return None
    if resolved_start >= resolved_end or resolved_end - resolved_start > _MAX_RANGE:
        return None
    return resolved_start, resolved_end


def _database(request: Request) -> Database | None:
    return request.app.state.database


async def usage_summary(
    request: Request,
    start: datetime | None = None,
    end: datetime | None = None,
    project_id: UUID | None = None,
    gateway_id: UUID | None = None,
) -> Response:
    authorization_error = authorize_operator(request)
    if authorization_error is not None:
        return authorization_error
    selected_range = _range(start, end)
    if selected_range is None:
        return JSONResponse(
            {"error": {"code": "INVALID_TIME_RANGE", "message": "Invalid time range"}},
            status_code=400,
        )
    database = _database(request)
    if database is None:
        return JSONResponse(
            {"error": {"code": "ANALYTICS_UNAVAILABLE", "message": "Analytics unavailable"}},
            status_code=503,
        )

    resolved_start, resolved_end = selected_range
    filters = [
        RequestRecord.started_at >= resolved_start,
        RequestRecord.started_at < resolved_end,
    ]
    if project_id is not None:
        filters.append(RequestRecord.project_id == project_id)
    if gateway_id is not None:
        filters.append(RequestRecord.gateway_id == gateway_id)
    statement = select(
        func.count().label("requests"),
        func.sum(case((RequestRecord.outcome == "succeeded", 1), else_=0)).label("successful"),
        func.sum(case((RequestRecord.outcome != "succeeded", 1), else_=0)).label("errors"),
        func.coalesce(func.sum(RequestRecord.prompt_tokens), 0).label("prompt_tokens"),
        func.coalesce(func.sum(RequestRecord.completion_tokens), 0).label("completion_tokens"),
        func.coalesce(func.sum(RequestRecord.total_tokens), 0).label("total_tokens"),
        func.coalesce(func.sum(RequestRecord.cost_microusd), 0).label("cost_microusd"),
        func.avg(RequestRecord.latency_ms).label("average_latency_ms"),
    ).where(*filters)
    async with database.sessions() as session:
        row = (await session.execute(statement)).one()
    return JSONResponse(
        {
            "start": resolved_start.isoformat(),
            "end": resolved_end.isoformat(),
            "requests": int(row.requests),
            "successful_requests": int(row.successful),
            "error_requests": int(row.errors),
            "prompt_tokens": int(row.prompt_tokens),
            "completion_tokens": int(row.completion_tokens),
            "total_tokens": int(row.total_tokens),
            "cost_microusd": int(row.cost_microusd),
            "average_latency_ms": round(float(row.average_latency_ms), 2)
            if row.average_latency_ms is not None
            else None,
        }
    )


async def usage_timeseries(
    request: Request,
    start: datetime | None = None,
    end: datetime | None = None,
    interval: Literal["hour", "day"] = "hour",
    project_id: UUID | None = None,
    gateway_id: UUID | None = None,
) -> Response:
    authorization_error = authorize_operator(request)
    if authorization_error is not None:
        return authorization_error
    selected_range = _range(start, end)
    if selected_range is None:
        return JSONResponse(
            {"error": {"code": "INVALID_TIME_RANGE", "message": "Invalid time range"}},
            status_code=400,
        )
    database = _database(request)
    if database is None:
        return JSONResponse(
            {"error": {"code": "ANALYTICS_UNAVAILABLE", "message": "Analytics unavailable"}},
            status_code=503,
        )

    resolved_start, resolved_end = selected_range
    filters = [
        RequestRecord.started_at >= resolved_start,
        RequestRecord.started_at < resolved_end,
    ]
    if project_id is not None:
        filters.append(RequestRecord.project_id == project_id)
    if gateway_id is not None:
        filters.append(RequestRecord.gateway_id == gateway_id)
    bucket = func.date_trunc(interval, RequestRecord.started_at).label("bucket")
    statement = (
        select(
            bucket,
            func.count().label("requests"),
            func.coalesce(func.sum(RequestRecord.total_tokens), 0).label("total_tokens"),
            func.coalesce(func.sum(RequestRecord.cost_microusd), 0).label("cost_microusd"),
            func.avg(RequestRecord.latency_ms).label("average_latency_ms"),
        )
        .where(*filters)
        .group_by(bucket)
        .order_by(bucket)
    )
    async with database.sessions() as session:
        rows = (await session.execute(statement)).all()
    return JSONResponse(
        {
            "start": resolved_start.isoformat(),
            "end": resolved_end.isoformat(),
            "interval": interval,
            "points": [
                {
                    "timestamp": row.bucket.isoformat(),
                    "requests": int(row.requests),
                    "total_tokens": int(row.total_tokens),
                    "cost_microusd": int(row.cost_microusd),
                    "average_latency_ms": round(float(row.average_latency_ms), 2)
                    if row.average_latency_ms is not None
                    else None,
                }
                for row in rows
            ],
        }
    )


async def usage_routes(
    request: Request,
    start: datetime | None = None,
    end: datetime | None = None,
    project_id: UUID | None = None,
    gateway_id: UUID | None = None,
) -> Response:
    authorization_error = authorize_operator(request)
    if authorization_error is not None:
        return authorization_error
    selected_range = _range(start, end)
    if selected_range is None:
        return JSONResponse(
            {"error": {"code": "INVALID_TIME_RANGE", "message": "Invalid time range"}},
            status_code=400,
        )
    database = _database(request)
    if database is None:
        return JSONResponse(
            {"error": {"code": "ANALYTICS_UNAVAILABLE", "message": "Analytics unavailable"}},
            status_code=503,
        )

    resolved_start, resolved_end = selected_range
    filters = [
        ProviderAttemptRecord.started_at >= resolved_start,
        ProviderAttemptRecord.started_at < resolved_end,
    ]
    if project_id is not None:
        filters.append(RequestRecord.project_id == project_id)
    if gateway_id is not None:
        filters.append(RequestRecord.gateway_id == gateway_id)
    statement = (
        select(
            ProviderAttemptRecord.route_id,
            Route.name.label("route_name"),
            ProviderAttemptRecord.provider,
            func.count().label("attempts"),
            func.sum(case((ProviderAttemptRecord.outcome == "succeeded", 1), else_=0)).label(
                "successful"
            ),
            func.sum(case((ProviderAttemptRecord.outcome == "started", 1), else_=0)).label(
                "in_flight"
            ),
            func.sum(
                case(
                    (
                        ProviderAttemptRecord.outcome.not_in(["succeeded", "started"]),
                        1,
                    ),
                    else_=0,
                )
            ).label("failed"),
            func.coalesce(func.sum(ProviderAttemptRecord.total_tokens), 0).label("total_tokens"),
            func.coalesce(func.sum(ProviderAttemptRecord.cost_microusd), 0).label("cost_microusd"),
            func.avg(ProviderAttemptRecord.latency_ms).label("average_latency_ms"),
        )
        .join(RequestRecord, RequestRecord.request_id == ProviderAttemptRecord.request_id)
        .outerjoin(Route, Route.id == ProviderAttemptRecord.route_id)
        .where(*filters)
        .group_by(ProviderAttemptRecord.route_id, Route.name, ProviderAttemptRecord.provider)
        .order_by(func.count().desc(), ProviderAttemptRecord.provider)
    )
    async with database.sessions() as session:
        rows = (await session.execute(statement)).all()
    total_attempts = sum(int(row.attempts) for row in rows)
    return JSONResponse(
        {
            "start": resolved_start.isoformat(),
            "end": resolved_end.isoformat(),
            "total_attempts": total_attempts,
            "routes": [
                {
                    "route_id": str(row.route_id) if row.route_id else None,
                    "route_name": row.route_name,
                    "provider": row.provider,
                    "attempts": int(row.attempts),
                    "attempt_share_percent": round(int(row.attempts) * 100 / total_attempts, 2)
                    if total_attempts
                    else 0,
                    "successful_attempts": int(row.successful),
                    "failed_attempts": int(row.failed),
                    "in_flight_attempts": int(row.in_flight),
                    "total_tokens": int(row.total_tokens),
                    "cost_microusd": int(row.cost_microusd),
                    "average_latency_ms": round(float(row.average_latency_ms), 2)
                    if row.average_latency_ms is not None
                    else None,
                }
                for row in rows
            ],
        }
    )


async def usage_tenants(
    request: Request,
    start: datetime | None = None,
    end: datetime | None = None,
    project_id: UUID | None = None,
    gateway_id: UUID | None = None,
) -> Response:
    authorization_error = authorize_operator(request)
    if authorization_error is not None:
        return authorization_error
    selected_range = _range(start, end)
    if selected_range is None:
        return JSONResponse(
            {"error": {"code": "INVALID_TIME_RANGE", "message": "Invalid time range"}},
            status_code=400,
        )
    database = _database(request)
    if database is None:
        return JSONResponse(
            {"error": {"code": "ANALYTICS_UNAVAILABLE", "message": "Analytics unavailable"}},
            status_code=503,
        )

    resolved_start, resolved_end = selected_range
    filters = [
        RequestRecord.started_at >= resolved_start,
        RequestRecord.started_at < resolved_end,
    ]
    if project_id is not None:
        filters.append(RequestRecord.project_id == project_id)
    if gateway_id is not None:
        filters.append(RequestRecord.gateway_id == gateway_id)
    tenant_id = RequestRecord.request_metadata["tenant_id"].as_string().label("tenant_id")
    tenant_trust = RequestRecord.request_metadata_trust["tenant_id"].as_string()
    statement = (
        select(
            tenant_id,
            func.count().label("requests"),
            func.sum(case((RequestRecord.outcome == "succeeded", 1), else_=0)).label("successful"),
            func.sum(case((RequestRecord.outcome == "started", 1), else_=0)).label("in_flight"),
            func.sum(
                case(
                    (RequestRecord.outcome.not_in(["succeeded", "started"]), 1),
                    else_=0,
                )
            ).label("errors"),
            func.coalesce(func.sum(RequestRecord.total_tokens), 0).label("total_tokens"),
            func.coalesce(func.sum(RequestRecord.cost_microusd), 0).label("cost_microusd"),
            func.avg(RequestRecord.latency_ms).label("average_latency_ms"),
        )
        .where(*filters, tenant_trust.in_(("fixed", "signed")))
        .group_by(tenant_id)
        .order_by(func.count().desc(), tenant_id.asc().nulls_last())
    )
    async with database.sessions() as session:
        rows = (await session.execute(statement)).all()
    return JSONResponse(
        {
            "start": resolved_start.isoformat(),
            "end": resolved_end.isoformat(),
            "tenants": [
                {
                    "tenant_id": row.tenant_id,
                    "requests": int(row.requests),
                    "successful_requests": int(row.successful),
                    "error_requests": int(row.errors),
                    "in_flight_requests": int(row.in_flight),
                    "success_rate_percent": round(
                        int(row.successful) * 100 / (int(row.successful) + int(row.errors)),
                        2,
                    )
                    if int(row.successful) + int(row.errors)
                    else 0,
                    "total_tokens": int(row.total_tokens),
                    "cost_microusd": int(row.cost_microusd),
                    "average_latency_ms": round(float(row.average_latency_ms), 2)
                    if row.average_latency_ms is not None
                    else None,
                }
                for row in rows
            ],
        }
    )


async def usage_attempts(request: Request, request_id: str) -> Response:
    authorization_error = authorize_operator(request)
    if authorization_error is not None:
        return authorization_error
    database = _database(request)
    if database is None:
        return JSONResponse(
            {"error": {"code": "ANALYTICS_UNAVAILABLE", "message": "Analytics unavailable"}},
            status_code=503,
        )
    statement = (
        select(ProviderAttemptRecord)
        .where(ProviderAttemptRecord.request_id == request_id)
        .order_by(ProviderAttemptRecord.attempt_index)
    )
    async with database.sessions() as session:
        attempts = (await session.scalars(statement)).all()
    if not attempts:
        return JSONResponse(
            {"error": {"code": "REQUEST_NOT_FOUND", "message": "Request not found"}},
            status_code=404,
        )
    return JSONResponse(
        {
            "request_id": request_id,
            "attempts": [
                {
                    "attempt_index": attempt.attempt_index,
                    "route_id": str(attempt.route_id) if attempt.route_id else None,
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
                    "completed_at": attempt.completed_at.isoformat()
                    if attempt.completed_at
                    else None,
                }
                for attempt in attempts
            ],
        }
    )


async def usage_requests(
    request: Request,
    metadata_key: str | None = None,
    metadata_value: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> Response:
    authorization_error = authorize_operator(request)
    if authorization_error is not None:
        return authorization_error
    if (metadata_key is None) != (metadata_value is None) or (
        metadata_key is not None
        and metadata_value is not None
        and (not _METADATA_KEY.fullmatch(metadata_key) or not 1 <= len(metadata_value) <= 256)
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
            {"error": {"code": "ANALYTICS_UNAVAILABLE", "message": "Analytics unavailable"}},
            status_code=503,
        )

    resolved_start, resolved_end = selected_range
    filters = [
        RequestRecord.started_at >= resolved_start,
        RequestRecord.started_at < resolved_end,
    ]
    if metadata_key is not None and metadata_value is not None:
        filters.append(RequestRecord.request_metadata[metadata_key].as_string() == metadata_value)
    statement = (
        select(RequestRecord)
        .where(*filters)
        .order_by(RequestRecord.started_at.desc(), RequestRecord.request_id.desc())
        .limit(limit + 1)
    )
    async with database.sessions() as session:
        records = list((await session.scalars(statement)).all())
    has_more = len(records) > limit
    records = records[:limit]
    return JSONResponse(
        {
            "start": resolved_start.isoformat(),
            "end": resolved_end.isoformat(),
            "metadata_key": metadata_key,
            "metadata_value": metadata_value,
            "has_more": has_more,
            "requests": [
                {
                    "request_id": record.request_id,
                    "model": record.model,
                    "provider": record.provider,
                    "outcome": record.outcome,
                    "http_status": record.http_status,
                    "error_code": record.error_code,
                    "estimated_tokens": record.estimated_tokens,
                    "prompt_tokens": record.prompt_tokens,
                    "completion_tokens": record.completion_tokens,
                    "total_tokens": record.total_tokens,
                    "cached_prompt_tokens": record.cached_prompt_tokens,
                    "cost_microusd": record.cost_microusd,
                    "cache_status": record.cache_status,
                    "metadata_trust": (record.request_metadata_trust or {}).get(metadata_key),
                    "latency_ms": record.latency_ms,
                    "started_at": record.started_at.isoformat(),
                    "completed_at": record.completed_at.isoformat()
                    if record.completed_at
                    else None,
                }
                for record in records
            ],
        }
    )
