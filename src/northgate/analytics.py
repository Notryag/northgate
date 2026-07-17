from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import compare_digest
from typing import Literal
from uuid import UUID

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import case, func, select

from northgate.config import Settings
from northgate.db.database import Database
from northgate.db.models import ProviderAttemptRecord, RequestRecord

_MAX_RANGE = timedelta(days=90)


def _authorize(request: Request) -> JSONResponse | None:
    settings: Settings = request.app.state.settings
    expected = settings.operator_key_sha256
    if expected is None or not expected.get_secret_value():
        return JSONResponse(
            {"error": {"code": "OPERATOR_AUTH_UNAVAILABLE", "message": "Operator API unavailable"}},
            status_code=503,
        )
    scheme, separator, credential = request.headers.get("authorization", "").partition(" ")
    actual = (
        sha256(credential.encode()).hexdigest() if separator and scheme.lower() == "bearer" else ""
    )
    if not compare_digest(actual, expected.get_secret_value()):
        return JSONResponse(
            {"error": {"code": "INVALID_OPERATOR_KEY", "message": "Invalid operator key"}},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return None


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
    authorization_error = _authorize(request)
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
    authorization_error = _authorize(request)
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


async def usage_attempts(request: Request, request_id: str) -> Response:
    authorization_error = _authorize(request)
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
