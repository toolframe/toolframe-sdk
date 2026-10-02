"""Optional runtime tracing. Importing the SDK never imports OpenTelemetry."""

from __future__ import annotations

import os

from .contracts import PluginError

_worker_tracing = None

# Forward only configuration consumed by the trace runtime. Build/preflight
# processes must not receive exporter credentials or activate instrumentation.
TRACE_ENVIRONMENT = frozenset(
    {
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_EXPORTER_OTLP_TIMEOUT",
        "OTEL_EXPORTER_OTLP_CERTIFICATE",
        "OTEL_EXPORTER_OTLP_CLIENT_KEY",
        "OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE",
        "OTEL_EXPORTER_OTLP_COMPRESSION",
        "OTEL_EXPORTER_OTLP_PROTOCOL",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
        "OTEL_EXPORTER_OTLP_TRACES_TIMEOUT",
        "OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE",
        "OTEL_EXPORTER_OTLP_TRACES_CLIENT_KEY",
        "OTEL_EXPORTER_OTLP_TRACES_CLIENT_CERTIFICATE",
        "OTEL_EXPORTER_OTLP_TRACES_COMPRESSION",
        "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
        "OTEL_TRACES_SAMPLER",
        "OTEL_TRACES_SAMPLER_ARG",
        "OTEL_RESOURCE_ATTRIBUTES",
        "OTEL_BSP_MAX_QUEUE_SIZE",
        "OTEL_BSP_MAX_EXPORT_BATCH_SIZE",
        "OTEL_BSP_SCHEDULE_DELAY",
        "OTEL_BSP_EXPORT_TIMEOUT",
        "OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT",
        "OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT",
        "OTEL_SPAN_EVENT_COUNT_LIMIT",
        "OTEL_SPAN_LINK_COUNT_LIMIT",
    }
)


def tracing_requested(environment=None):
    environment = os.environ if environment is None else environment
    value = environment.get("TOOLFRAME_TRACING_ENABLED", "false").strip().lower()
    if value not in {"", "false", "0", "true", "1"}:
        raise PluginError(
            "tracing_config_invalid", "TOOLFRAME_TRACING_ENABLED must be true or false"
        )
    return value in {"true", "1"}


def configure_tracing(service_name, attributes=None, *, install_global=False):
    """Initialize one process; applications without the extra remain importable."""
    if (
        not tracing_requested()
        or os.environ.get("OTEL_SDK_DISABLED", "").lower() == "true"
    ):
        return None
    try:
        from ._tracing import create_tracing
    except ImportError as error:
        raise PluginError(
            "tracing_unavailable",
            "Tracing requires toolframe-sdk[tracing]; install the extra or disable TOOLFRAME_TRACING_ENABLED",
        ) from error
    tracing = create_tracing(
        service_name, attributes or {}, install_global=install_global
    )
    if install_global:
        global _worker_tracing
        _worker_tracing = tracing
    return tracing


def worker_tracing():
    """The worker's initialized runtime, also used by optional HTTP providers."""
    return _worker_tracing


class TracingMiddleware:
    """Trace HTTP requests without requiring a framework instrumentation package."""

    def __init__(self, app, tracing):
        self.app, self.tracing = app, tracing

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") == "/healthz":
            return await self.app(scope, receive, send)
        method = scope.get("method", "HTTP")
        carrier = {
            key.decode("latin1"): value.decode("latin1")
            for key, value in scope.get("headers", ())
            if key.lower() in {b"traceparent", b"tracestate"}
        }
        with self.tracing.span(
            method,
            kind="server",
            carrier=carrier,
            attributes={"http.request.method": method},
        ) as span:

            async def traced_send(message):
                if message["type"] == "http.response.start":
                    status = message["status"]
                    span.set_attribute("http.response.status_code", status)
                    route = getattr(scope.get("route"), "path", None)
                    if route:
                        span.set_attribute("http.route", route)
                        span.update_name(f"{method} {route}")
                    if status >= 500:
                        self.tracing.error(span, str(status))
                await send(message)

            await self.app(scope, receive, traced_send)
