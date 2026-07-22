import argparse
import json
import os
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

import httpx

EXIT_HEALTHY = 0
EXIT_FINDINGS = 2
EXIT_AUTH = 3
EXIT_SERVICE = 4


class InspectError(Exception):
    pass


class InspectAuthError(InspectError):
    pass


class InspectServiceError(InspectError):
    pass


@dataclass(frozen=True)
class InspectConfig:
    base_url: str
    operator_key: str
    timeout_seconds: float = 30.0

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "InspectConfig":
        values = os.environ if environ is None else environ
        base_url = values.get("NORTHGATE_INSPECT_BASE_URL", "").strip()
        inline_key = values.get("NORTHGATE_INSPECT_OPERATOR_KEY", "").strip()
        key_file = values.get("NORTHGATE_INSPECT_OPERATOR_KEY_FILE", "").strip()
        if not base_url:
            raise InspectError("NORTHGATE_INSPECT_BASE_URL is required")
        try:
            parsed_url = httpx.URL(base_url)
        except httpx.InvalidURL as exc:
            raise InspectError("NORTHGATE_INSPECT_BASE_URL must be a valid URL") from exc
        if parsed_url.scheme not in ("http", "https") or not parsed_url.host:
            raise InspectError("NORTHGATE_INSPECT_BASE_URL must be an HTTP or HTTPS URL")
        if inline_key and key_file:
            raise InspectError(
                "Set only one of NORTHGATE_INSPECT_OPERATOR_KEY and "
                "NORTHGATE_INSPECT_OPERATOR_KEY_FILE"
            )
        operator_key = inline_key or _read_operator_key(Path(key_file) if key_file else None)
        if not operator_key:
            raise InspectError(
                "NORTHGATE_INSPECT_OPERATOR_KEY or NORTHGATE_INSPECT_OPERATOR_KEY_FILE is required"
            )
        try:
            timeout_seconds = float(values.get("NORTHGATE_INSPECT_TIMEOUT_SECONDS", "30"))
        except ValueError as exc:
            raise InspectError("NORTHGATE_INSPECT_TIMEOUT_SECONDS must be a number") from exc
        if timeout_seconds <= 0 or timeout_seconds > 300:
            raise InspectError("NORTHGATE_INSPECT_TIMEOUT_SECONDS must be between 0 and 300")
        return cls(
            base_url=base_url.rstrip("/"),
            operator_key=operator_key,
            timeout_seconds=timeout_seconds,
        )


def _read_operator_key(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        details = path.stat()
        if not stat.S_ISREG(details.st_mode):
            raise InspectError("NORTHGATE_INSPECT_OPERATOR_KEY_FILE must be a regular file")
        if details.st_size > 4096:
            raise InspectError("NORTHGATE_INSPECT_OPERATOR_KEY_FILE is too large")
        if details.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise InspectError(
                "NORTHGATE_INSPECT_OPERATOR_KEY_FILE must not be accessible by group or others"
            )
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise InspectError("Could not read NORTHGATE_INSPECT_OPERATOR_KEY_FILE") from exc
    if not value:
        raise InspectError("NORTHGATE_INSPECT_OPERATOR_KEY_FILE is empty")
    return value


class OperatorDiagnosticsClient:
    def __init__(self, config: InspectConfig, *, client: httpx.Client | None = None) -> None:
        self.config = config
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=config.timeout_seconds,
            follow_redirects=False,
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def inspect_request(self, request_id: str) -> dict[str, object]:
        return self._get(f"/api/v1/diagnostics/requests/{request_id}")

    def inspect_correlated(
        self,
        *,
        metadata_key: str,
        metadata_value: str,
        start: str | None,
        end: str | None,
        limit: int,
    ) -> dict[str, object]:
        params: dict[str, str | int] = {
            "metadata_key": metadata_key,
            "metadata_value": metadata_value,
            "limit": limit,
        }
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end
        return self._get("/api/v1/diagnostics/correlated", params=params)

    def _get(
        self,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
    ) -> dict[str, object]:
        try:
            response = self.client.get(
                f"{self.config.base_url}{path}",
                params=params,
                headers={"Authorization": f"Bearer {self.config.operator_key}"},
            )
        except httpx.TimeoutException as exc:
            raise InspectServiceError("Diagnostics request timed out") from exc
        except httpx.TransportError as exc:
            raise InspectServiceError("Could not reach the Northgate Operator API") from exc
        if response.status_code in (401, 403):
            raise InspectAuthError("Operator authentication failed")
        if response.status_code >= 400:
            code = _error_code(response)
            suffix = f" ({code})" if code is not None else ""
            raise InspectServiceError(f"Operator API returned HTTP {response.status_code}{suffix}")
        try:
            payload = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise InspectServiceError("Operator API returned invalid JSON") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise InspectServiceError("Operator API returned an unsupported diagnostics schema")
        return payload


def _error_code(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("error"), dict):
        return None
    code = payload["error"].get("code")
    return code if isinstance(code, str) else None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="northgate-inspect")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="Inspect requests sharing a correlation value")
    run.add_argument("correlation_value")
    run.add_argument("--metadata-key", default="run_id")
    run.add_argument("--start")
    run.add_argument("--end")
    run.add_argument("--limit", type=int, default=50, choices=range(1, 101), metavar="1..100")
    run.add_argument("--json", action="store_true", dest="json_output")

    request = commands.add_parser("request", help="Inspect one Northgate request")
    request.add_argument("request_id")
    request.add_argument("--json", action="store_true", dest="json_output")
    return parser


def _finding_count(payload: dict[str, object]) -> int:
    findings = payload.get("findings")
    return len(findings) if isinstance(findings, list) else 0


def _human_request(payload: dict[str, object], output: TextIO) -> None:
    request = payload.get("request")
    if not isinstance(request, dict):
        raise InspectServiceError("Diagnostics response is missing request data")
    print(f"Request: {request.get('request_id')}", file=output)
    print(
        f"Outcome: {request.get('outcome')}  HTTP: {request.get('http_status')}  "
        f"Provider: {request.get('provider')}",
        file=output,
    )
    print(
        f"Tokens: prompt={request.get('prompt_tokens')} "
        f"completion={request.get('completion_tokens')} total={request.get('total_tokens')} "
        f"cached={request.get('cached_prompt_tokens')}",
        file=output,
    )
    attempts = payload.get("attempts")
    settlement = payload.get("settlement")
    events = settlement.get("events") if isinstance(settlement, dict) else None
    print(
        f"Attempts: {len(attempts) if isinstance(attempts, list) else 0}  "
        f"Settlement events: {len(events) if isinstance(events, list) else 0}",
        file=output,
    )
    _human_findings(payload, output)


def _human_run(payload: dict[str, object], output: TextIO) -> None:
    correlation = payload.get("correlation")
    aggregate = payload.get("aggregate")
    if not isinstance(correlation, dict) or not isinstance(aggregate, dict):
        raise InspectServiceError("Diagnostics response is missing correlation aggregates")
    print(
        f"Correlation: {correlation.get('metadata_key')}={correlation.get('metadata_value')}",
        file=output,
    )
    print(
        f"Requests: {aggregate.get('requests')}  Total tokens: {aggregate.get('total_tokens')}  "
        f"Missing usage: {aggregate.get('usage_missing_requests')}  "
        f"Cost (microusd): {aggregate.get('cost_microusd')}",
        file=output,
    )
    if payload.get("has_more") is True:
        print("Result truncated: increase --limit or narrow the time range", file=output)
    _human_findings(payload, output)


def _human_findings(payload: dict[str, object], output: TextIO) -> None:
    finding_counts = payload.get("finding_counts")
    if isinstance(finding_counts, dict):
        if finding_counts:
            print("Findings:", file=output)
            for code, count in sorted(finding_counts.items()):
                print(f"  {code}: {count}", file=output)
        else:
            print("Findings: none", file=output)
        return
    findings = payload.get("findings")
    if not isinstance(findings, list) or not findings:
        print("Findings: none", file=output)
        return
    print("Findings:", file=output)
    for finding in findings:
        if isinstance(finding, dict):
            print(f"  {finding.get('severity')}: {finding.get('code')}", file=output)


def run_cli(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    client: httpx.Client | None = None,
    output: TextIO = sys.stdout,
    error: TextIO = sys.stderr,
) -> int:
    args = _parser().parse_args(argv)
    try:
        config = InspectConfig.from_environment(environ)
        diagnostics = OperatorDiagnosticsClient(config, client=client)
        try:
            if args.command == "run":
                payload = diagnostics.inspect_correlated(
                    metadata_key=args.metadata_key,
                    metadata_value=args.correlation_value,
                    start=args.start,
                    end=args.end,
                    limit=args.limit,
                )
            else:
                payload = diagnostics.inspect_request(args.request_id)
        finally:
            diagnostics.close()
        if args.json_output:
            print(json.dumps(payload, sort_keys=True), file=output)
        elif args.command == "run":
            _human_run(payload, output)
        else:
            _human_request(payload, output)
        return EXIT_FINDINGS if _finding_count(payload) else EXIT_HEALTHY
    except InspectAuthError as exc:
        print(f"AUTH: {exc}", file=error)
        return EXIT_AUTH
    except InspectError as exc:
        print(f"FAILED: {exc}", file=error)
        return EXIT_SERVICE


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
