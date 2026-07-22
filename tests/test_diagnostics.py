import json
from datetime import UTC, datetime
from uuid import uuid4

from northgate.db.models import ProviderAttemptRecord, RequestRecord, SettlementEvent
from northgate.diagnostics import build_correlated_diagnostic, build_request_diagnostic


def _request(
    *,
    request_id: str,
    outcome: str,
    total_tokens: int | None,
    cached_prompt_tokens: int | None,
    metadata_trust: dict[str, str] | None,
) -> RequestRecord:
    now = datetime.now(UTC)
    return RequestRecord(
        request_id=request_id,
        provider="openai",
        model="gpt-test",
        request_metadata={"run_id": "secret-correlation-value"},
        request_metadata_trust=metadata_trust,
        cost_microusd=17 if total_tokens is not None else None,
        outcome=outcome,
        http_status=200,
        prompt_tokens=2449 if total_tokens is not None else None,
        completion_tokens=14 if total_tokens is not None else None,
        total_tokens=total_tokens,
        cached_prompt_tokens=cached_prompt_tokens,
        estimated_tokens=3000,
        cache_status="bypass",
        latency_ms=7400 if outcome != "started" else None,
        first_token_ms=200 if outcome != "started" else None,
        started_at=now,
        completed_at=now if outcome != "started" else None,
    )


def _attempt(*, request_id: str, outcome: str, total_tokens: int | None) -> ProviderAttemptRecord:
    now = datetime.now(UTC)
    return ProviderAttemptRecord(
        id=uuid4(),
        request_id=request_id,
        attempt_index=1,
        provider="openai",
        outcome=outcome,
        http_status=200,
        prompt_tokens=2449 if total_tokens is not None else None,
        completion_tokens=14 if total_tokens is not None else None,
        total_tokens=total_tokens,
        cached_prompt_tokens=0 if total_tokens is not None else None,
        cost_microusd=17 if total_tokens is not None else None,
        latency_ms=7400 if outcome != "started" else None,
        started_at=now,
        completed_at=now if outcome != "started" else None,
    )


def _event(request_id: str) -> SettlementEvent:
    now = datetime.now(UTC)
    return SettlementEvent(
        id=uuid4(),
        request_id=request_id,
        event_key="terminal",
        payload={"schema_version": 1, "private": "must-not-be-returned"},
        status="completed",
        attempts=1,
        database_settled_at=now,
        policy_settled_at=now,
        created_at=now,
        completed_at=now,
    )


def test_started_terminal_request_without_event_reports_accounting_gap() -> None:
    request_id = "req-incident"
    result = build_request_diagnostic(
        _request(
            request_id=request_id,
            outcome="started",
            total_tokens=None,
            cached_prompt_tokens=None,
            metadata_trust=None,
        ),
        [_attempt(request_id=request_id, outcome="started", total_tokens=None)],
        [],
        settlement_expected=True,
    )

    codes = {finding["code"] for finding in result["findings"]}
    assert {
        "REQUEST_STILL_STARTED",
        "ATTEMPT_STILL_STARTED",
        "TERMINAL_HTTP_WITHOUT_SETTLEMENT",
        "USAGE_MISSING",
        "METADATA_TRUST_MISSING",
    } <= codes
    assert "secret-correlation-value" not in json.dumps(result)


def test_completed_request_reports_only_informational_cache_findings() -> None:
    request_id = "req-healthy"
    result = build_request_diagnostic(
        _request(
            request_id=request_id,
            outcome="succeeded",
            total_tokens=2463,
            cached_prompt_tokens=0,
            metadata_trust={"run_id": "untrusted"},
        ),
        [_attempt(request_id=request_id, outcome="succeeded", total_tokens=2463)],
        [_event(request_id)],
        settlement_expected=True,
    )

    assert {finding["code"] for finding in result["findings"]} == {
        "PROMPT_CACHE_NOT_HIT",
        "EXACT_CACHE_BYPASSED",
    }
    assert {finding["severity"] for finding in result["findings"]} == {"info"}
    assert "must-not-be-returned" not in json.dumps(result)
    assert result["settlement"]["events"][0]["schema_version"] == 1


def test_correlated_diagnostic_aggregates_known_usage_and_missing_records() -> None:
    healthy = build_request_diagnostic(
        _request(
            request_id="req-healthy",
            outcome="succeeded",
            total_tokens=2463,
            cached_prompt_tokens=0,
            metadata_trust={"run_id": "untrusted"},
        ),
        [_attempt(request_id="req-healthy", outcome="succeeded", total_tokens=2463)],
        [_event("req-healthy")],
        settlement_expected=True,
    )
    incomplete = build_request_diagnostic(
        _request(
            request_id="req-incomplete",
            outcome="started",
            total_tokens=None,
            cached_prompt_tokens=None,
            metadata_trust=None,
        ),
        [],
        [],
        settlement_expected=True,
    )
    now = datetime.now(UTC)

    result = build_correlated_diagnostic(
        [healthy, incomplete],
        metadata_key="run_id",
        metadata_value="run-test",
        start=now,
        end=now,
        has_more=False,
    )

    assert result["aggregate"] == {
        "requests": 2,
        "prompt_tokens": 2449,
        "completion_tokens": 14,
        "total_tokens": 2463,
        "cached_prompt_tokens": 0,
        "cost_microusd": 17,
        "usage_missing_requests": 1,
        "prompt_cache_percent": 0.0,
    }
    assert result["finding_counts"]["REQUEST_STILL_STARTED"] == 1
