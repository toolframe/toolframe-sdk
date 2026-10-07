"""OpenTelemetry implementation, loaded only when tracing is explicitly enabled."""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import contextmanager
from urllib.parse import urlsplit

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from . import __version__
from .contracts import PluginError
from .tracing import TRACE_ENVIRONMENT


class _ClosingExporter(SpanExporter):
    """Stop draining a backlog once shutdown's best-effort export budget expires."""

    def __init__(self, exporter):
        self.exporter = exporter
        self.deadline = None

    def export(self, spans):
        if self.deadline is not None and time.monotonic() >= self.deadline:
            return SpanExportResult.FAILURE
        return self.exporter.export(spans)

    def shutdown(self):
        self.exporter.shutdown()


class Tracing:
    """A process-owned provider and the RPC/HTTP instrumentation that uses it."""

    def __init__(self, provider, exporter=None, logs=None):
        self.provider = provider
        self.exporter = exporter
        self.logs = logs
        self.closed = False
        self.tracer = provider.get_tracer("toolframe", __version__)
        self.propagator = TraceContextTextMapPropagator()
        self.environment = {
            key: value for key, value in os.environ.items() if key in TRACE_ENVIRONMENT
        }

    def child_environment(self, service_name):
        return {
            **self.environment,
            "TOOLFRAME_TRACING_ENABLED": "true",
            "OTEL_SERVICE_NAME": service_name,
        }

    def inject(self, carrier=None):
        carrier = {} if carrier is None else carrier
        self.propagator.inject(carrier)
        return carrier

    @contextmanager
    def span(self, name, *, kind="internal", attributes=None, carrier=None):
        parent = None
        if carrier is not None:
            # The reader task is long-lived. Never inherit its context for an
            # incoming request, even if a malformed/missing carrier is supplied.
            clean = (
                {
                    key: value
                    for key, value in carrier.items()
                    if key in {"traceparent", "tracestate"}
                    and isinstance(value, str)
                    and len(value) <= 512
                }
                if isinstance(carrier, dict)
                else {}
            )
            parent = self.propagator.extract(clean, context=Context())
        with self.tracer.start_as_current_span(
            name,
            context=parent,
            kind={
                "internal": SpanKind.INTERNAL,
                "client": SpanKind.CLIENT,
                "server": SpanKind.SERVER,
            }[kind],
            attributes=attributes,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                yield span
            except BaseException as error:
                category = (
                    error.code
                    if isinstance(error, PluginError)
                    else type(error).__name__
                )
                self.error(span, category)
                if isinstance(error, asyncio.CancelledError):
                    span.set_attribute("toolframe.cancelled", True)
                # Exception messages and stack locals can contain invocation
                # payloads or credentials. Record the category, never the data.
                span.add_event("exception", {"exception.type": category})
                raise

    def rpc_span(self, method, params, *, kind, carrier=None, attributes=None):
        values = {"rpc.system": "jsonrpc", "rpc.method": method, **(attributes or {})}
        context = params.get("context")
        context = context if isinstance(context, dict) else {}
        root = params.get("root_id") or context.get("root_id")
        if isinstance(root, str):
            values["toolframe.root.id"] = root[:128]
        name = method
        for key, attribute in (
            ("service", "toolframe.service.name"),
            ("method", "toolframe.method.name"),
            ("alias", "toolframe.dependency.alias"),
            ("key", "toolframe.resource.key"),
        ):
            value = params.get(key)
            if isinstance(value, str):
                values[attribute] = value[:256]
        if method == "runtime.invoke":
            name = f"invoke {values.get('toolframe.service.name', '')}/{values.get('toolframe.method.name', '')}"
        return self.span(name, kind=kind, attributes=values, carrier=carrier)

    @staticmethod
    def error(span, category):
        span.set_attribute("error.type", category[:128])
        span.set_status(Status(StatusCode.ERROR))

    @staticmethod
    def log_correlation():
        context = trace.get_current_span().get_span_context()
        if not context.is_valid:
            return {}
        return {
            "trace_id": f"{context.trace_id:032x}",
            "span_id": f"{context.span_id:016x}",
        }

    def shutdown(self):
        if self.closed:
            return
        self.closed = True
        deadline = time.monotonic() + 2
        if self.exporter:
            self.exporter.deadline = deadline
        if self.logs:
            self.logs.exporter.deadline = deadline
        self.provider.shutdown()
        if self.logs:
            self.logs.shutdown()


def create_tracing(service_name, attributes, *, install_global=False):
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.environ.get(
        "OTEL_EXPORTER_OTLP_ENDPOINT", ""
    )
    protocol = os.environ.get(
        "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
        os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf"),
    )
    try:
        address = urlsplit(endpoint)
        valid_endpoint = (
            address.scheme in {"http", "https"}
            and address.hostname
            and not address.username
            and not address.password
        )
    except ValueError:
        valid_endpoint = False
    if not valid_endpoint or protocol != "http/protobuf":
        raise PluginError(
            "tracing_config_invalid",
            "Tracing requires an HTTP(S) OTLP endpoint and the http/protobuf protocol; use OTEL_EXPORTER_OTLP_TRACES_HEADERS for credentials",
        )
    try:
        queue = int(os.environ.get("OTEL_BSP_MAX_QUEUE_SIZE", "512"))
        batch = int(
            os.environ.get("OTEL_BSP_MAX_EXPORT_BATCH_SIZE", str(min(128, queue)))
        )
        delay = int(os.environ.get("OTEL_BSP_SCHEDULE_DELAY", "1000"))
        timeout = float(
            os.environ.get(
                "OTEL_EXPORTER_OTLP_TRACES_TIMEOUT",
                os.environ.get("OTEL_EXPORTER_OTLP_TIMEOUT", "2"),
            )
        )
        if not (queue > 0 and 0 < batch <= queue and delay > 0 and 0 < timeout <= 5):
            raise ValueError()
    except ValueError:
        raise PluginError(
            "tracing_config_invalid",
            "Tracing needs a positive queue/delay, a batch no larger than the queue, and an exporter timeout in (0, 5] seconds",
        ) from None
    resource = Resource.create(
        {
            "service.name": os.environ.get("OTEL_SERVICE_NAME") or service_name,
            **attributes,
        }
    )
    logs_exporter = os.environ.get("OTEL_LOGS_EXPORTER", "none").strip().lower()
    if logs_exporter not in {"", "none", "otlp"}:
        raise PluginError(
            "logging_config_invalid", "OTEL_LOGS_EXPORTER must be none or otlp"
        )
    logs = None
    if logs_exporter == "otlp":
        from ._logging import create_logs

        logs = create_logs(resource)
    provider, exporter = None, None
    try:
        exporter = _ClosingExporter(OTLPSpanExporter(timeout=timeout))
        provider = TracerProvider(resource=resource, shutdown_on_exit=False)
        processor = BatchSpanProcessor(
            exporter,
            max_queue_size=queue,
            max_export_batch_size=batch,
            schedule_delay_millis=delay,
        )
        provider.add_span_processor(processor)
    except BaseException:
        if provider:
            provider.shutdown()
        if exporter:
            exporter.shutdown()
        if logs:
            logs.shutdown()
        raise
    if install_global:
        trace.set_tracer_provider(provider)
    return Tracing(provider, exporter, logs)
