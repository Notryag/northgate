import argparse
import json
import os
import re
import stat
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TextIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

EXIT_HEALTHY = 0
EXIT_FINDINGS = 2
EXIT_AUTH = 3
EXIT_SERVICE = 4
_DURATION = re.compile(r"^(\d+)([smh]?)$")
_SINCE_DURATION = re.compile(r"^(\d+)([mhd])$")
_TODAY_AT = re.compile(r"^today@(\d{2}):(\d{2})$")


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
    credential_source: str = "environment"

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
            credential_source="environment" if inline_key else "file",
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

    def inspect_stale(self, *, minimum_age_seconds: int, limit: int) -> dict[str, object]:
        return self._get(
            "/api/v1/diagnostics/stale",
            params={"minimum_age_seconds": minimum_age_seconds, "limit": limit},
        )

    def inspect_usage(
        self,
        *,
        metadata_key: str,
        metadata_value: str,
        group_by: str | None,
        start: str,
        end: str,
        limit: int,
    ) -> dict[str, object]:
        params: dict[str, str | int] = {
            "metadata_key": metadata_key,
            "metadata_value": metadata_value,
            "start": start,
            "end": end,
            "limit": limit,
        }
        if group_by is not None:
            params["group_by"] = group_by
        return self._get("/api/v1/diagnostics/usage", params=params)

    def capabilities(self) -> dict[str, object]:
        return self._get("/api/v1/diagnostics/capabilities")

    def resolve_application(self, name_or_id: str) -> str:
        payload = self._get_json("/api/v1/application-keys", require_schema=False)
        if not isinstance(payload, list):
            raise InspectServiceError("Operator API returned an invalid application list")
        matches = [
            item
            for item in payload
            if isinstance(item, dict)
            and (item.get("id") == name_or_id or item.get("name") == name_or_id)
        ]
        if not matches:
            raise InspectServiceError("Application was not found")
        exact_ids = [item for item in matches if item.get("id") == name_or_id]
        selected = exact_ids or matches
        if len(selected) != 1 or not isinstance(selected[0].get("id"), str):
            raise InspectServiceError("Application name is ambiguous; use its application ID")
        return str(selected[0]["id"])

    def _get(
        self,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
    ) -> dict[str, object]:
        payload = self._get_json(path, params=params)
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise InspectServiceError("Operator API returned an unsupported diagnostics schema")
        return payload

    def _get_json(
        self,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        require_schema: bool = True,
    ) -> object:
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
        if require_schema and (not isinstance(payload, dict) or payload.get("schema_version") != 1):
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

    stale = commands.add_parser("stale", help="Inspect stale accounting and policy state")
    stale.add_argument("--minimum-age", type=_duration_seconds, default=300, metavar="DURATION")
    stale.add_argument("--limit", type=int, default=100, choices=range(1, 101), metavar="1..100")
    stale.add_argument("--json", action="store_true", dest="json_output")

    usage = commands.add_parser("usage", help="Aggregate a bounded metadata-filtered time range")
    usage.add_argument("--metadata-key", required=True)
    usage.add_argument("--metadata-value", required=True)
    usage.add_argument("--group-by")
    usage_range = usage.add_mutually_exclusive_group()
    usage_range.add_argument("--start")
    usage_range.add_argument("--since")
    usage.add_argument("--end", default="now")
    usage.add_argument("--timezone", default="UTC")
    usage.add_argument("--limit", type=int, default=100, choices=range(1, 101), metavar="1..100")
    usage.add_argument("--json", action="store_true", dest="json_output")

    recent = commands.add_parser(
        "recent", help="List recent grouped correlations for an application"
    )
    recent.add_argument("--application", required=True)
    recent.add_argument("--group-by", default="run_id")
    recent.add_argument("--since", default="2h")
    recent.add_argument("--timezone", default="UTC")
    recent.add_argument("--limit", type=int, default=100, choices=range(1, 101), metavar="1..100")
    recent.add_argument("--json", action="store_true", dest="json_output")

    doctor = commands.add_parser(
        "doctor", help="Check configuration and Operator API compatibility"
    )
    doctor.add_argument("--json", action="store_true", dest="json_output")
    return parser


def _duration_seconds(value: str) -> int:
    match = _DURATION.fullmatch(value)
    if match is None:
        raise argparse.ArgumentTypeError("duration must be an integer with optional s, m, or h")
    amount = int(match.group(1))
    multiplier = {"": 1, "s": 1, "m": 60, "h": 3600}[match.group(2)]
    seconds = amount * multiplier
    if seconds < 30 or seconds > 86400:
        raise argparse.ArgumentTypeError("duration must be between 30s and 24h")
    return seconds


def _timezone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise InspectError(f"Unknown timezone: {value}") from exc


def _time(value: str, *, timezone: ZoneInfo, now: datetime) -> datetime:
    if value == "now":
        return now
    today_match = _TODAY_AT.fullmatch(value)
    if today_match is not None:
        hour, minute = (int(part) for part in today_match.groups())
        if hour > 23 or minute > 59:
            raise InspectError("today@ time must use HH:MM")
        return now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InspectError(f"Invalid time: {value}") from exc
    return parsed.replace(tzinfo=timezone) if parsed.tzinfo is None else parsed


def _time_range(
    *,
    start: str | None,
    since: str | None,
    end: str,
    timezone_name: str,
    default_since: timedelta,
) -> tuple[str, str]:
    timezone = _timezone(timezone_name)
    now = datetime.now(timezone)
    resolved_end = _time(end, timezone=timezone, now=now)
    if start is not None:
        resolved_start = _time(start, timezone=timezone, now=now)
    elif since is not None:
        match = _SINCE_DURATION.fullmatch(since)
        if match is not None:
            amount = int(match.group(1))
            multiplier = {"m": 60, "h": 3600, "d": 86400}[match.group(2)]
            resolved_start = resolved_end - timedelta(seconds=amount * multiplier)
        else:
            resolved_start = _time(since, timezone=timezone, now=now)
    else:
        resolved_start = resolved_end - default_since
    if resolved_start >= resolved_end or resolved_end - resolved_start > timedelta(days=90):
        raise InspectError("Time range must be positive and no longer than 90 days")
    return resolved_start.isoformat(), resolved_end.isoformat()


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
    print(
        f"Reservation: prompt={request.get('estimated_prompt_tokens')} "
        f"output={request.get('reserved_output_tokens')} "
        f"attempts={request.get('attempt_multiplier')} "
        f"margin={request.get('reservation_margin_tokens')} "
        f"reserved={request.get('reserved_total_tokens')} "
        f"actual={request.get('actual_total_tokens')} "
        f"released={request.get('released_tokens')} "
        f"ratio={request.get('estimate_actual_ratio')}",
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


def _human_stale(payload: dict[str, object], output: TextIO) -> None:
    requests = payload.get("requests")
    if not isinstance(requests, list):
        raise InspectServiceError("Diagnostics response is missing stale requests")
    print(
        f"Stale requests: {len(requests)}  Minimum age: {payload.get('minimum_age_seconds')}s  "
        f"Policy state: {'available' if payload.get('policy_state_available') else 'unavailable'}",
        file=output,
    )
    if payload.get("has_more") is True or payload.get("policy_keys_truncated") is True:
        print("Result truncated: narrow the query or inspect policy state directly", file=output)
    _human_findings(payload, output)


def _human_usage(payload: dict[str, object], output: TextIO, *, timezone: str) -> None:
    aggregate = payload.get("aggregate")
    filter_value = payload.get("filter")
    groups = payload.get("groups")
    if not isinstance(aggregate, dict) or not isinstance(filter_value, dict):
        raise InspectServiceError("Diagnostics response is missing usage aggregates")
    cache_percent = aggregate.get("prompt_cache_percent")
    cache_label = "at least " if aggregate.get("prompt_cache_percent_is_lower_bound") else ""
    cache_display = f"{cache_label}{cache_percent}%" if cache_percent is not None else "unknown"
    print(
        f"Range: {payload.get('start')} to {payload.get('end')}  Timezone: {timezone}", file=output
    )
    print(
        f"Filter: {filter_value.get('metadata_key')}={filter_value.get('metadata_value')}  "
        f"Requests: {aggregate.get('requests')}  "
        f"Groups: {len(groups) if isinstance(groups, list) else 0}",
        file=output,
    )
    print(
        f"Tokens: prompt={aggregate.get('prompt_tokens')} "
        f"completion={aggregate.get('completion_tokens')} total={aggregate.get('total_tokens')}  "
        f"Cached prompt: {aggregate.get('cached_prompt_tokens')} "
        f"({cache_display})  "
        f"Missing cache detail: {aggregate.get('cached_usage_missing_requests')}",
        file=output,
    )
    print(
        f"Reservation sample: requests={aggregate.get('reservation_sample_requests')} "
        f"reserved={aggregate.get('reserved_total_tokens')} "
        f"actual={aggregate.get('actual_total_tokens')} "
        f"released={aggregate.get('released_tokens')} "
        f"ratio={aggregate.get('estimate_actual_ratio')}",
        file=output,
    )
    if isinstance(groups, list) and groups:
        print(f"Grouped by {payload.get('group_by')}:", file=output)
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("aggregate"), dict):
                continue
            group_aggregate = group["aggregate"]
            trust = ",".join(group.get("metadata_trust", [])) or "unknown"
            print(
                f"  {group.get('metadata_value')}: requests={group_aggregate.get('requests')} "
                f"total={group_aggregate.get('total_tokens')} "
                f"cached={group_aggregate.get('cached_prompt_tokens')} trust={trust}",
                file=output,
            )
    requests = payload.get("requests")
    if isinstance(requests, list) and requests:
        print("Requests:", file=output)
        for diagnostic in requests:
            if not isinstance(diagnostic, dict) or not isinstance(diagnostic.get("request"), dict):
                continue
            request = diagnostic["request"]
            attempts = diagnostic.get("attempts")
            print(
                f"  {request.get('request_id')}: outcome={request.get('outcome')} "
                f"model={request.get('model')} latency_ms={request.get('latency_ms')} "
                f"attempts={len(attempts) if isinstance(attempts, list) else 0}",
                file=output,
            )
    if payload.get("has_more") is True:
        print(
            "Result truncated: totals and groups cover only the returned request page", file=output
        )
    _human_findings_by_severity(payload, output)


def _human_findings_by_severity(payload: dict[str, object], output: TextIO) -> None:
    findings = payload.get("findings")
    if not isinstance(findings, list) or not findings:
        print("Findings: none", file=output)
        return
    grouped: dict[str, Counter[str]] = {}
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        severity = str(finding.get("severity", "unknown"))
        code = str(finding.get("code", "UNKNOWN"))
        grouped.setdefault(severity, Counter())[code] += 1
    print("Findings by severity:", file=output)
    for severity in ("error", "warning", "info", "unknown"):
        if severity not in grouped:
            continue
        values = ", ".join(f"{code}={count}" for code, count in sorted(grouped[severity].items()))
        print(f"  {severity}: {values}", file=output)


def _doctor_payload(config: InspectConfig, capabilities: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": capabilities["schema_version"],
        "base_url": config.base_url,
        "credential_source": config.credential_source,
        "credential_valid": True,
        "operator_api_reachable": True,
        "operator_api_authorized": True,
        "compatible": capabilities.get("schema_version") == 1,
        "capabilities": capabilities.get("capabilities", []),
    }


def _human_doctor(payload: dict[str, object], output: TextIO) -> None:
    print(f"Base URL: {payload.get('base_url')}", file=output)
    print(f"Credential source: {payload.get('credential_source')}", file=output)
    print("Credential validation: passed", file=output)
    print("Operator API: reachable and authorized", file=output)
    print(f"Diagnostics schema: {payload.get('schema_version')} (compatible)", file=output)


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
            elif args.command == "request":
                payload = diagnostics.inspect_request(args.request_id)
            elif args.command == "stale":
                payload = diagnostics.inspect_stale(
                    minimum_age_seconds=args.minimum_age,
                    limit=args.limit,
                )
            elif args.command == "doctor":
                payload = _doctor_payload(config, diagnostics.capabilities())
            elif args.command == "usage":
                start, end = _time_range(
                    start=args.start,
                    since=args.since,
                    end=args.end,
                    timezone_name=args.timezone,
                    default_since=timedelta(hours=24),
                )
                payload = diagnostics.inspect_usage(
                    metadata_key=args.metadata_key,
                    metadata_value=args.metadata_value,
                    group_by=args.group_by,
                    start=start,
                    end=end,
                    limit=args.limit,
                )
            else:
                start, end = _time_range(
                    start=None,
                    since=args.since,
                    end="now",
                    timezone_name=args.timezone,
                    default_since=timedelta(hours=2),
                )
                application_id = diagnostics.resolve_application(args.application)
                payload = diagnostics.inspect_usage(
                    metadata_key="northgate.application_id",
                    metadata_value=application_id,
                    group_by=args.group_by,
                    start=start,
                    end=end,
                    limit=args.limit,
                )
        finally:
            diagnostics.close()
        if args.json_output:
            print(json.dumps(payload, sort_keys=True), file=output)
        elif args.command == "run":
            _human_run(payload, output)
        elif args.command == "stale":
            _human_stale(payload, output)
        elif args.command in ("usage", "recent"):
            _human_usage(payload, output, timezone=args.timezone)
        elif args.command == "doctor":
            _human_doctor(payload, output)
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
