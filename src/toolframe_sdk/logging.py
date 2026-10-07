"""Structured plugin logging with request-local labels and standard log methods."""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import traceback
from contextlib import contextmanager
from datetime import UTC, datetime

_context = contextvars.ContextVar("toolframe_log_context", default=None)
_record_fields = frozenset(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
    "trace_id",
    "span_id",
}
_identity_fields = frozenset(
    {
        "service.name",
        "service.namespace",
        "service.instance.id",
        "toolframe.application.id",
        "toolframe.plugin.id",
        "toolframe.plugin.generation",
    }
)


@contextmanager
def log_context(attributes, *, replace=False):
    """Scope labels to this async call; managed tasks can start a fresh scope."""
    token = _context.set({**({} if replace else (_context.get() or {})), **attributes})
    try:
        yield
    finally:
        _context.reset(token)


def record_attributes(record):
    attributes = {
        key: value
        for key, value in record.__dict__.items()
        if key not in _record_fields
    }
    attributes.update(
        {
            "code.function.name": record.funcName,
            "code.namespace": record.module,
            "code.file.path": record.pathname,
            "code.line.number": record.lineno,
        }
    )
    if record.exc_info and record.exc_info[1] is not None:
        error = record.exc_info[1]
        attributes.update(
            {
                "exception.type": type(error).__qualname__,
                "exception.message": str(error),
                "exception.stacktrace": "".join(
                    traceback.format_exception(*record.exc_info)
                ),
            }
        )
    return attributes


class ContextLogger(logging.LoggerAdapter):
    """A stdlib logger adapter; bind adds immutable labels to a child adapter."""

    def process(self, message, kwargs):
        values = {**self.extra, **(_context.get() or {}), **(kwargs.get("extra") or {})}
        # Identity is assigned by the runtime, independently of per-call labels.
        values.update(
            {key: value for key, value in self.extra.items() if key in _identity_fields}
        )
        kwargs["extra"] = values
        return message, kwargs

    def bind(self, **attributes):
        values = {**self.extra, **attributes}
        values.update(
            {key: value for key, value in self.extra.items() if key in _identity_fields}
        )
        return ContextLogger(self.logger, values)


def plugin_logger(plugin_id, generation, *, level=logging.INFO):
    logger = logging.getLogger(f"toolframe.plugin.{plugin_id}")
    logger.setLevel(level)
    return ContextLogger(
        logger,
        {
            "toolframe.plugin.id": plugin_id,
            "toolframe.plugin.generation": generation,
        },
    )


class _ContextFilter(logging.Filter):
    def __init__(self, attributes, correlation):
        super().__init__()
        self.attributes, self.correlation = attributes, correlation

    def filter(self, record):
        values = {**(_context.get() or {}), **self.attributes}
        # Preserve explicit labels emitted by ContextLogger, except process identity.
        for key, value in values.items():
            if key in _identity_fields or key not in record.__dict__:
                record.__dict__[key] = value
        if self.correlation:
            record.__dict__.update(self.correlation())
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record):
        value = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "attributes": record_attributes(record),
        }
        for key in ("trace_id", "span_id"):
            if key in record.__dict__:
                value[key] = record.__dict__[key]
        return json.dumps(value, ensure_ascii=False, default=str)


class LoggingSession:
    """Own only the handlers installed for one Core instance or worker process."""

    def __init__(self, logger, attributes=None, *, tracing=None):
        self.logger = logger
        self.handlers = []
        self.attributes = dict(attributes or {})
        context_filter = _ContextFilter(
            self.attributes, tracing.log_correlation if tracing else None
        )
        local = logging.StreamHandler(sys.stderr)
        local.setLevel(logging.INFO)
        local.setFormatter(_JsonFormatter())
        handlers = [local]
        if tracing and tracing.logs:
            handlers.append(tracing.logs.handler)
        for handler in handlers:
            handler.addFilter(context_filter)
            logger.addHandler(handler)
            self.handlers.append(handler)
        self.log = ContextLogger(logger, self.attributes)

    def close(self):
        for handler in self.handlers:
            self.logger.removeHandler(handler)
            handler.close()
        self.handlers.clear()
