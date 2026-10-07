"""Optional OTLP log export. Imported only when the logs signal is enabled."""

from __future__ import annotations

import contextvars
import logging
import os
import time
from urllib.parse import urlsplit

from opentelemetry._logs import SeverityNumber
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    LogRecordExporter,
    LogRecordExportResult,
)

from .contracts import PluginError
from .logging import record_attributes

_exporting = contextvars.ContextVar("toolframe_exporting_logs", default=False)


class _ClosingExporter(LogRecordExporter):
    def __init__(self, exporter):
        self.exporter, self.deadline = exporter, None

    def export(self, batch):
        if self.deadline is not None and time.monotonic() >= self.deadline:
            return LogRecordExportResult.FAILURE
        token = _exporting.set(True)
        try:
            return self.exporter.export(batch)
        finally:
            _exporting.reset(token)

    def shutdown(self):
        self.exporter.shutdown()

    def force_flush(self, timeout_millis=30000):
        return self.exporter.force_flush(timeout_millis)


class _Handler(logging.Handler):
    def __init__(self, provider, level):
        super().__init__(level)
        self.provider = provider

    def emit(self, record):
        # Exporter diagnostics must never recursively enter their own queue.
        if _exporting.get() or record.name.startswith("opentelemetry"):
            return
        try:
            severity = next(
                severity
                for threshold, severity in (
                    (logging.CRITICAL, SeverityNumber.FATAL),
                    (logging.ERROR, SeverityNumber.ERROR),
                    (logging.WARNING, SeverityNumber.WARN),
                    (logging.INFO, SeverityNumber.INFO),
                    (logging.DEBUG, SeverityNumber.DEBUG),
                    (0, SeverityNumber.UNSPECIFIED),
                )
                if record.levelno >= threshold
            )
            self.provider.get_logger(record.name).emit(
                timestamp=int(record.created * 1_000_000_000),
                severity_number=severity,
                severity_text=record.levelname,
                body=record.getMessage(),
                attributes=record_attributes(record),
            )
        except Exception:  # noqa: BLE001 - a logging sink must not fail business calls
            # Malformed application log data must not fail a business operation.
            self.handleError(record)


class Logs:
    def __init__(self, provider, exporter, level):
        self.provider, self.exporter = provider, exporter
        self.handler = _Handler(provider, level)

    def shutdown(self):
        self.handler.close()
        self.provider.shutdown()


def create_logs(resource):
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT") or os.environ.get(
        "OTEL_EXPORTER_OTLP_ENDPOINT", ""
    )
    protocol = os.environ.get(
        "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL",
        os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf"),
    )
    try:
        address = urlsplit(endpoint)
        if not (
            address.scheme in {"http", "https"}
            and address.hostname
            and not address.username
            and not address.password
            and protocol == "http/protobuf"
        ):
            raise ValueError()
        queue = int(os.environ.get("OTEL_BLRP_MAX_QUEUE_SIZE", "512"))
        batch = int(
            os.environ.get("OTEL_BLRP_MAX_EXPORT_BATCH_SIZE", str(min(128, queue)))
        )
        delay = int(os.environ.get("OTEL_BLRP_SCHEDULE_DELAY", "1000"))
        timeout = float(
            os.environ.get(
                "OTEL_EXPORTER_OTLP_LOGS_TIMEOUT",
                os.environ.get("OTEL_EXPORTER_OTLP_TIMEOUT", "2"),
            )
        )
        level_name = os.environ.get("TOOLFRAME_LOGS_LEVEL", "INFO").strip().upper()
        level = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}[
            level_name
        ]
        if not (queue > 0 and 0 < batch <= queue and delay > 0 and 0 < timeout <= 5):
            raise ValueError()
    except ValueError, KeyError:
        raise PluginError(
            "logging_config_invalid",
            "Logs require an HTTP(S) OTLP endpoint with http/protobuf, a positive queue/delay, "
            "a batch no larger than the queue, a timeout in (0, 5] seconds, and "
            "TOOLFRAME_LOGS_LEVEL=DEBUG, INFO, WARNING, ERROR or CRITICAL",
        ) from None
    exporter = _ClosingExporter(OTLPLogExporter(timeout=timeout))
    try:
        provider = LoggerProvider(resource=resource, shutdown_on_exit=False)
        provider.add_log_record_processor(
            BatchLogRecordProcessor(
                exporter,
                max_queue_size=queue,
                max_export_batch_size=batch,
                schedule_delay_millis=delay,
            )
        )
    except BaseException:
        exporter.shutdown()
        raise
    return Logs(provider, exporter, level)
