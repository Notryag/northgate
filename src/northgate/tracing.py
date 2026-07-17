from dataclasses import dataclass

from opentelemetry import context, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
)
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import Span, SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from northgate.config import Settings


@dataclass(frozen=True)
class ServerSpan:
    span: Span
    context_token: object
    method: str


class Tracing:
    def __init__(
        self,
        settings: Settings,
        version: str,
        *,
        span_exporter: SpanExporter | None = None,
    ) -> None:
        if span_exporter is None:
            if not settings.otlp_traces_endpoint:
                raise ValueError(
                    "NORTHGATE_OTLP_TRACES_ENDPOINT is required when tracing is enabled"
                )
            span_exporter = OTLPSpanExporter(
                endpoint=settings.otlp_traces_endpoint,
                timeout=settings.trace_export_timeout_seconds,
            )
            processor = BatchSpanProcessor(
                span_exporter,
                export_timeout_millis=settings.trace_export_timeout_seconds * 1000,
            )
        else:
            processor = SimpleSpanProcessor(span_exporter)

        self.provider = TracerProvider(
            sampler=ParentBased(TraceIdRatioBased(settings.trace_sample_ratio)),
            resource=Resource.create(
                {
                    "service.name": settings.trace_service_name,
                    "service.version": version,
                }
            ),
            shutdown_on_exit=False,
        )
        self.provider.add_span_processor(processor)
        self.tracer = self.provider.get_tracer("northgate", version)
        self.propagator = TraceContextTextMapPropagator()

    def start_server_span(
        self,
        *,
        method: str,
        headers: list[tuple[bytes, bytes]],
    ) -> ServerSpan:
        carrier = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in headers}
        parent_context = self.propagator.extract(carrier)
        span = self.tracer.start_span(
            f"HTTP {method}",
            context=parent_context,
            kind=SpanKind.SERVER,
            attributes={"http.request.method": method},
        )
        token = context.attach(trace.set_span_in_context(span, parent_context))
        return ServerSpan(span=span, context_token=token, method=method)

    def finish_server_span(
        self,
        handle: ServerSpan,
        *,
        route: str,
        status_code: int,
        request_id: str,
    ) -> None:
        handle.span.update_name(f"{handle.method} {route}")
        handle.span.set_attribute("http.route", route)
        handle.span.set_attribute("http.response.status_code", status_code)
        handle.span.set_attribute("northgate.request_id", request_id)
        if status_code >= 500:
            handle.span.set_status(Status(StatusCode.ERROR))
        handle.span.end()
        context.detach(handle.context_token)

    def inject(self, headers: dict[str, str]) -> None:
        self.propagator.inject(headers)

    def shutdown(self) -> None:
        self.provider.shutdown()

    def force_flush(self) -> bool:
        return self.provider.force_flush()


def add_span_event(name: str, attributes: dict[str, str | int | float | bool | None]) -> None:
    span = trace.get_current_span()
    if not span.is_recording():
        return
    span.add_event(name, {key: value for key, value in attributes.items() if value is not None})
