"""The plugin logger interface retains identity, call sites and async isolation."""

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from toolframe_sdk import Plugin, PluginError, RuntimeContext
from toolframe_sdk.logging import LoggingSession, log_context
from toolframe_sdk.tracing import configure_tracing


async def test_bound_loggers_keep_identity_call_sites_and_concurrent_context(capsys):
    root = logging.getLogger()
    previous = list(root.handlers)
    session = LoggingSession(root, {"toolframe.application.id": "app"})
    first = RuntimeContext(None, plugin_id="first", generation="g1")
    second = RuntimeContext(None, plugin_id="second", generation="g2")
    assert Plugin(first).log is first.log
    bound = first.log.bind(component="repository", **{"toolframe.plugin.id": "spoof"})

    async def emit(context, method, logger):
        with log_context({"toolframe.method.name": method}):
            await asyncio.sleep(0)
            logger.info(
                "Rows loaded", extra={"row_count": 2, "toolframe.plugin.id": "spoof"}
            )

    try:
        await asyncio.gather(
            emit(first, "read", bound), emit(second, "write", second.log)
        )
        first.log.info("Outside call")
    finally:
        session.close()
    assert root.handlers == previous
    records = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    rows = {
        record["attributes"]["toolframe.plugin.id"]: record for record in records[:2]
    }
    assert rows["first"]["attributes"]["toolframe.method.name"] == "read"
    assert rows["second"]["attributes"]["toolframe.method.name"] == "write"
    assert rows["first"]["attributes"]["component"] == "repository"
    assert "component" not in rows["second"]["attributes"]
    assert all(
        row["attributes"]["code.function.name"] == "emit" for row in rows.values()
    )
    assert all(row["attributes"]["code.line.number"] > 0 for row in rows.values())
    assert "toolframe.method.name" not in records[-1]["attributes"]
    assert "component" not in records[-1]["attributes"]


@pytest.mark.parametrize("fail", [False, True])
async def test_managed_task_discards_call_labels_and_keeps_its_own_root(capsys, fail):
    async def request(method, params):
        return {"root_id": "task-root"}

    context = RuntimeContext(
        SimpleNamespace(request=request, tracing=None),
        plugin_id="tasks",
        generation="g",
    )
    session = LoggingSession(logging.getLogger())
    context.ready.set()

    async def work():
        context.log.info("Refresh running")
        if fail:
            raise ValueError("private-error-message")

    try:
        with log_context(
            {"toolframe.method.name": "activate", "toolframe.root.id": "old"}
        ):
            if fail:
                with pytest.raises(ValueError):
                    await context.task(work, name="refresh")
            else:
                await context.task(work, name="refresh")
        await context.close()
    finally:
        session.close()
    output = capsys.readouterr().err
    assert "private-error-message" not in output
    records = [json.loads(line) for line in output.splitlines()]
    assert len(records) == (2 if fail else 1)
    for record in records:
        attributes = record["attributes"]
        assert attributes["toolframe.root.id"] == "task-root"
        assert attributes["toolframe.task.name"] == "refresh"
        assert "toolframe.method.name" not in attributes


@pytest.mark.parametrize(
    "settings",
    [
        {"OTEL_LOGS_EXPORTER": "console"},
        {"OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": ""},
        {
            "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "http://user:secret@localhost:4318/v1/logs"
        },
        {"OTEL_EXPORTER_OTLP_LOGS_PROTOCOL": "grpc"},
        {"OTEL_EXPORTER_OTLP_LOGS_TIMEOUT": "0"},
        {"OTEL_EXPORTER_OTLP_LOGS_TIMEOUT": "nan"},
        {"OTEL_BLRP_MAX_QUEUE_SIZE": "0"},
        {"OTEL_BLRP_MAX_EXPORT_BATCH_SIZE": "513"},
        {"OTEL_BLRP_SCHEDULE_DELAY": "0"},
        {"TOOLFRAME_LOGS_LEVEL": "INVALID"},
    ],
)
def test_invalid_log_settings_are_actionable_without_echoing_secrets(
    monkeypatch, settings
):
    monkeypatch.setenv("TOOLFRAME_TRACING_ENABLED", "true")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://localhost:4318/v1/traces"
    )
    monkeypatch.setenv("OTEL_LOGS_EXPORTER", "otlp")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "http://localhost:4318/v1/logs"
    )
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(PluginError) as error:
        configure_tracing("test")
    assert error.value.code == "logging_config_invalid"
    assert "secret" not in str(error.value)


def test_logs_require_master_enable_and_explicit_exporter(monkeypatch):
    monkeypatch.setenv("OTEL_LOGS_EXPORTER", "otlp")
    monkeypatch.setenv("TOOLFRAME_TRACING_ENABLED", "false")
    assert configure_tracing("test") is None
    monkeypatch.setenv("TOOLFRAME_TRACING_ENABLED", "true")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    assert configure_tracing("test") is None
    monkeypatch.delenv("OTEL_SDK_DISABLED")
    monkeypatch.setenv("OTEL_LOGS_EXPORTER", "none")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://localhost:4318/v1/traces"
    )
    tracing = configure_tracing("test")
    try:
        assert tracing.logs is None
    finally:
        tracing.shutdown()


def test_unsampled_logs_filter_levels_keep_context_and_suppress_export_recursion(
    monkeypatch,
):
    from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter

    from toolframe_sdk import _logging

    class Exporter(InMemoryLogRecordExporter):
        def export(self, batch):
            logging.getLogger("collector.transport").warning("Exporter diagnostic")
            return super().export(batch)

    exporter = Exporter()
    monkeypatch.setattr(_logging, "OTLPLogExporter", lambda **kwargs: exporter)
    monkeypatch.setenv("TOOLFRAME_TRACING_ENABLED", "true")
    monkeypatch.setenv("OTEL_LOGS_EXPORTER", "otlp")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "always_off")
    monkeypatch.setenv("TOOLFRAME_LOGS_LEVEL", "WARNING")
    tracing = configure_tracing("example", {"toolframe.plugin.id": "example"})
    root = logging.getLogger()
    session = LoggingSession(root, {"toolframe.plugin.id": "example"}, tracing=tracing)
    context = RuntimeContext(None, plugin_id="example", generation="g1")
    try:
        with tracing.span("unsampled") as span:
            assert not span.is_recording()
            context.log.info("Below export threshold")
            context.log.warning("Retained warning")
            logging.getLogger("opentelemetry.exporter.test").warning("SDK diagnostic")
        context.log.warning("Outside span")
        assert tracing.logs.provider.force_flush(timeout_millis=1000)
        records = [item.log_record for item in exporter.get_finished_logs()]
        assert [record.body for record in records] == [
            "Retained warning",
            "Outside span",
        ]
        assert records[0].trace_id == span.get_span_context().trace_id
        assert records[0].span_id == span.get_span_context().span_id
        assert records[1].trace_id == 0 and records[1].span_id == 0
    finally:
        session.close()
        tracing.shutdown()
