import time
from hashlib import sha256
from hmac import compare_digest

from fastapi import Request
from fastapi.responses import Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    GCCollector,
    Histogram,
    PlatformCollector,
    ProcessCollector,
    generate_latest,
)


class Metrics:
    def __init__(self, version: str) -> None:
        self.registry = CollectorRegistry()
        GCCollector(registry=self.registry)
        PlatformCollector(registry=self.registry)
        ProcessCollector(registry=self.registry)

        self.build_info = Gauge(
            "northgate_build_info",
            "Northgate build information.",
            ("version",),
            registry=self.registry,
        )
        self.build_info.labels(version=version).set(1)
        self.http_requests = Counter(
            "northgate_http_requests_total",
            "HTTP requests completed by Northgate.",
            ("method", "route", "status_code"),
            registry=self.registry,
        )
        self.http_request_duration = Histogram(
            "northgate_http_request_duration_seconds",
            "End-to-end HTTP request duration.",
            ("method", "route"),
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 120),
            registry=self.registry,
        )
        self.http_in_progress = Gauge(
            "northgate_http_requests_in_progress",
            "HTTP requests currently being handled.",
            registry=self.registry,
        )
        self.gateway_errors = Counter(
            "northgate_gateway_errors_total",
            "Stable gateway errors returned before or between provider attempts.",
            ("code",),
            registry=self.registry,
        )
        self.provider_attempts = Counter(
            "northgate_provider_attempts_total",
            "Provider calls completed by outcome.",
            ("provider", "adapter", "outcome"),
            registry=self.registry,
        )
        self.provider_attempt_duration = Histogram(
            "northgate_provider_attempt_duration_seconds",
            "Provider attempt duration.",
            ("provider", "adapter"),
            buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 120),
            registry=self.registry,
        )
        self.provider_tokens = Counter(
            "northgate_provider_tokens_total",
            "Provider-reported tokens across completed attempts.",
            ("provider", "adapter", "type"),
            registry=self.registry,
        )
        self.provider_cost_microusd = Counter(
            "northgate_provider_cost_microusd_total",
            "Calculated provider cost in millionths of a US dollar.",
            ("provider", "adapter"),
            registry=self.registry,
        )
        self.cache_requests = Counter(
            "northgate_cache_requests_total",
            "Exact-cache request outcomes.",
            ("result",),
            registry=self.registry,
        )
        self.cache_writes = Counter(
            "northgate_cache_writes_total",
            "Exact-cache write outcomes.",
            ("result",),
            registry=self.registry,
        )
        self.route_skips = Counter(
            "northgate_route_skips_total",
            "Routes skipped without a provider call.",
            ("provider", "adapter", "reason"),
            registry=self.registry,
        )

    def observe_http(
        self,
        *,
        method: str,
        route: str,
        status_code: int,
        started_at: float,
    ) -> None:
        self.http_requests.labels(
            method=method,
            route=route,
            status_code=str(status_code),
        ).inc()
        self.http_request_duration.labels(method=method, route=route).observe(
            time.perf_counter() - started_at
        )

    def observe_provider_attempt(
        self,
        *,
        provider: str,
        adapter: str,
        outcome: str,
        duration_seconds: float,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        cost_microusd: int | None,
    ) -> None:
        self.provider_attempts.labels(
            provider=provider,
            adapter=adapter,
            outcome=outcome,
        ).inc()
        self.provider_attempt_duration.labels(provider=provider, adapter=adapter).observe(
            duration_seconds
        )
        if prompt_tokens is not None and prompt_tokens >= 0:
            self.provider_tokens.labels(provider=provider, adapter=adapter, type="prompt").inc(
                prompt_tokens
            )
        if completion_tokens is not None and completion_tokens >= 0:
            self.provider_tokens.labels(provider=provider, adapter=adapter, type="completion").inc(
                completion_tokens
            )
        if cost_microusd is not None and cost_microusd >= 0:
            self.provider_cost_microusd.labels(provider=provider, adapter=adapter).inc(
                cost_microusd
            )


def metrics_response(request: Request) -> Response:
    settings = request.app.state.settings
    expected = settings.metrics_key_sha256
    if expected is not None and expected.get_secret_value():
        scheme, separator, credential = request.headers.get("authorization", "").partition(" ")
        actual = (
            sha256(credential.encode()).hexdigest()
            if separator and scheme.lower() == "bearer"
            else ""
        )
        if not compare_digest(actual, expected.get_secret_value()):
            return Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
    metrics: Metrics = request.app.state.metrics
    return Response(
        content=generate_latest(metrics.registry),
        headers={"Content-Type": CONTENT_TYPE_LATEST},
    )
