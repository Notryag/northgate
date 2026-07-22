import json
from io import StringIO

import httpx
import pytest

from northgate.inspect import (
    EXIT_AUTH,
    EXIT_FINDINGS,
    EXIT_HEALTHY,
    EXIT_SERVICE,
    InspectConfig,
    InspectError,
    run_cli,
)


def _environment(**overrides: str) -> dict[str, str]:
    return {
        "NORTHGATE_INSPECT_BASE_URL": "https://northgate.test",
        "NORTHGATE_INSPECT_OPERATOR_KEY": "operator-secret",
        **overrides,
    }


def _correlated_payload(*, findings: bool = False) -> dict[str, object]:
    finding_list = (
        [
            {
                "code": "REQUEST_STILL_STARTED",
                "severity": "error",
                "request_id": "req_12345678",
            }
        ]
        if findings
        else []
    )
    return {
        "schema_version": 1,
        "correlation": {"metadata_key": "run_id", "metadata_value": "run-test"},
        "has_more": False,
        "aggregate": {
            "requests": 1,
            "total_tokens": 5,
            "usage_missing_requests": 0,
            "cost_microusd": 7,
        },
        "finding_counts": {"REQUEST_STILL_STARTED": 1} if findings else {},
        "requests": [],
        "findings": finding_list,
    }


def _request_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "request": {
            "request_id": "req_12345678",
            "outcome": "succeeded",
            "http_status": 200,
            "provider": "openai",
            "prompt_tokens": 3,
            "completion_tokens": 2,
            "total_tokens": 5,
            "cached_prompt_tokens": 0,
        },
        "attempts": [{"attempt_index": 1}],
        "settlement": {"events": [{"status": "completed"}]},
        "findings": [],
    }


def _stale_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "minimum_age_seconds": 300,
        "has_more": False,
        "policy_state_available": True,
        "policy_keys_truncated": False,
        "finding_counts": {"UNPROTECTED_STALE_SETTLEMENT": 1},
        "requests": [{"request": {"request_id": "req_12345678"}}],
        "findings": [
            {
                "code": "UNPROTECTED_STALE_SETTLEMENT",
                "severity": "error",
                "request_id": "req_12345678",
            }
        ],
    }


def test_run_json_uses_operator_api_and_returns_findings_exit_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/diagnostics/correlated"
        assert request.url.params["metadata_key"] == "run_id"
        assert request.url.params["metadata_value"] == "run-test"
        assert request.url.params["limit"] == "25"
        assert request.headers["Authorization"] == "Bearer operator-secret"
        return httpx.Response(200, json=_correlated_payload(findings=True))

    output = StringIO()
    error = StringIO()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        exit_code = run_cli(
            ["run", "run-test", "--limit", "25", "--json"],
            environ=_environment(),
            client=client,
            output=output,
            error=error,
        )

    assert exit_code == EXIT_FINDINGS
    assert json.loads(output.getvalue())["schema_version"] == 1
    assert error.getvalue() == ""
    assert "operator-secret" not in output.getvalue()


def test_request_human_output_returns_healthy_exit_code() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=_request_payload(), request=request)
        )
    )
    output = StringIO()
    try:
        exit_code = run_cli(
            ["request", "req_12345678"],
            environ=_environment(),
            client=client,
            output=output,
            error=StringIO(),
        )
    finally:
        client.close()

    assert exit_code == EXIT_HEALTHY
    assert "Request: req_12345678" in output.getvalue()
    assert "Tokens: prompt=3 completion=2 total=5 cached=0" in output.getvalue()
    assert "Findings: none" in output.getvalue()


def test_stale_command_parses_duration_and_uses_bounded_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/diagnostics/stale"
        assert request.url.params["minimum_age_seconds"] == "300"
        assert request.url.params["limit"] == "20"
        return httpx.Response(200, json=_stale_payload())

    output = StringIO()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        exit_code = run_cli(
            ["stale", "--minimum-age", "5m", "--limit", "20"],
            environ=_environment(),
            client=client,
            output=output,
            error=StringIO(),
        )

    assert exit_code == EXIT_FINDINGS
    assert "Stale requests: 1" in output.getvalue()
    assert "UNPROTECTED_STALE_SETTLEMENT: 1" in output.getvalue()


@pytest.mark.parametrize("status_code", [401, 403])
def test_authentication_failure_has_distinct_exit_code(status_code: int) -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(status_code, request=request))
    )
    error = StringIO()
    try:
        exit_code = run_cli(
            ["request", "req_12345678"],
            environ=_environment(),
            client=client,
            output=StringIO(),
            error=error,
        )
    finally:
        client.close()

    assert exit_code == EXIT_AUTH
    assert error.getvalue().startswith("AUTH:")
    assert "operator-secret" not in error.getvalue()


def test_service_error_and_unsupported_schema_have_service_exit_code() -> None:
    responses = iter(
        [
            httpx.Response(
                503,
                json={"error": {"code": "DIAGNOSTICS_UNAVAILABLE"}},
            ),
            httpx.Response(200, json={"schema_version": 2}),
        ]
    )
    client = httpx.Client(transport=httpx.MockTransport(lambda _request: next(responses)))
    try:
        first_error = StringIO()
        assert (
            run_cli(
                ["request", "req_12345678"],
                environ=_environment(),
                client=client,
                output=StringIO(),
                error=first_error,
            )
            == EXIT_SERVICE
        )
        second_error = StringIO()
        assert (
            run_cli(
                ["request", "req_12345678"],
                environ=_environment(),
                client=client,
                output=StringIO(),
                error=second_error,
            )
            == EXIT_SERVICE
        )
    finally:
        client.close()

    assert "DIAGNOSTICS_UNAVAILABLE" in first_error.getvalue()
    assert "unsupported diagnostics schema" in second_error.getvalue()


def test_operator_key_file_must_have_private_permissions(tmp_path) -> None:
    key_file = tmp_path / "operator-key"
    key_file.write_text("file-secret\n", encoding="utf-8")
    key_file.chmod(0o644)
    environment = {
        "NORTHGATE_INSPECT_BASE_URL": "https://northgate.test",
        "NORTHGATE_INSPECT_OPERATOR_KEY_FILE": str(key_file),
    }

    with pytest.raises(InspectError, match="must not be accessible"):
        InspectConfig.from_environment(environment)

    key_file.chmod(0o600)
    config = InspectConfig.from_environment(environment)
    assert config.operator_key == "file-secret"


def test_missing_configuration_does_not_fall_back_to_process_environment() -> None:
    with pytest.raises(InspectError, match="BASE_URL"):
        InspectConfig.from_environment({})
