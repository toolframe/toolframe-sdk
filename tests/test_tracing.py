"""Trace continuity and isolation across the real concurrent RPC transport."""

import asyncio
import json
import os
import socket
import subprocess
import sys
from collections import defaultdict

import pytest
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ParentBased
from opentelemetry.trace import SpanKind, StatusCode
from test_rpc import peers

from toolframe_sdk import PluginError, RuntimeContext
from toolframe_sdk._tracing import Tracing
from toolframe_sdk.tracing import configure_tracing, tracing_requested


@pytest.fixture
def telemetry():
    exporter = InMemorySpanExporter()
    providers = []

    def make(service, *, sampler=None):
        provider = TracerProvider(
            resource=Resource.create({"service.name": service}),
            sampler=sampler,
            shutdown_on_exit=False,
        )
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        providers.append(provider)
        return Tracing(provider)

    yield make, exporter
    for provider in providers:
        provider.shutdown()


def test_disabled_runtime_does_not_import_opentelemetry():
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("OTEL_")
    }
    environment["TOOLFRAME_TRACING_ENABLED"] = "false"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            """
import sys
import toolframe_sdk
from toolframe_sdk import http, rpc, worker
from toolframe_sdk.tracing import configure_tracing
assert configure_tracing('disabled') is None
assert not any(name.startswith('opentelemetry') for name in sys.modules)
""",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "value,expected",
    [("", False), ("false", False), ("0", False), (" TRUE ", True), ("1", True)],
)
def test_explicit_feature_flag(value, expected):
    assert tracing_requested({"TOOLFRAME_TRACING_ENABLED": value}) is expected


def test_invalid_feature_flag_is_actionable():
    with pytest.raises(PluginError, match="must be true or false"):
        tracing_requested({"TOOLFRAME_TRACING_ENABLED": "typo"})


async def test_parallel_nested_calls_keep_separate_parent_chains(telemetry):
    make, exporter = telemetry
    first, second = make("caller"), make("worker")

    async def local(method, params):
        await asyncio.sleep(0)
        return params["number"] * 2

    async def remote(method, params):
        await asyncio.sleep(0)
        return await right.request("nested", params)

    async with peers(local, remote) as (left, right):
        left.tracing, right.tracing = first, second
        assert await asyncio.gather(
            *(left.request("work", {"number": n}) for n in range(20))
        ) == [n * 2 for n in range(20)]
    traces = defaultdict(list)
    for span in exporter.get_finished_spans():
        traces[span.context.trace_id].append(span)
    assert len(traces) == 20
    for spans in traces.values():
        assert len(spans) == 4
        root = next(span for span in spans if span.parent is None)
        assert root.name == "work" and root.kind == SpanKind.CLIENT
        chain = [root]
        while children := [
            span
            for span in spans
            if span.parent and span.parent.span_id == chain[-1].context.span_id
        ]:
            assert len(children) == 1
            chain.extend(children)
        assert [span.kind for span in chain] == [
            SpanKind.CLIENT,
            SpanKind.SERVER,
            SpanKind.CLIENT,
            SpanKind.SERVER,
        ]
        assert [span.resource.attributes["service.name"] for span in chain] == [
            "caller",
            "worker",
            "worker",
            "caller",
        ]
    assert not trace.get_current_span().get_span_context().is_valid


async def test_error_and_oversized_response_end_both_spans_without_payloads(telemetry):
    make, exporter = telemetry
    secret = "do-not-export-this-password"

    async def handler(method, params):
        if method == "large":
            return "x" * (1024 * 1024)
        raise PluginError("business_failure", secret, {"token": secret})

    async with peers(handler, handler) as (left, right):
        left.tracing, right.tracing = make("caller"), make("worker")
        for method, code in [
            ("fail", "business_failure"),
            ("large", "frame_too_large"),
        ]:
            with pytest.raises(PluginError) as error:
                await left.request(method, {"password": secret})
            assert error.value.code == code
    spans = exporter.get_finished_spans()
    assert len(spans) == 4
    assert all(span.status.status_code == StatusCode.ERROR for span in spans)
    assert secret not in "".join(span.to_json() for span in spans)
    assert {span.attributes["error.type"] for span in spans} == {
        "business_failure",
        "frame_too_large",
    }


async def test_timeout_records_client_timeout_and_remote_cancellation(telemetry):
    make, exporter = telemetry
    cancelled = asyncio.Event()

    async def handler(method, params):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async with peers(handler, handler) as (left, right):
        left.tracing, right.tracing = make("caller"), make("worker")
        with pytest.raises(TimeoutError):
            await left.request("slow", timeout=0.03)
        await asyncio.wait_for(cancelled.wait(), 1)
    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    assert {span.attributes["error.type"] for span in spans} == {
        "TimeoutError",
        "CancelledError",
    }
    assert any(span.attributes.get("toolframe.cancelled") for span in spans)


async def test_unsampled_parent_is_propagated_without_recording(telemetry):
    make, exporter = telemetry
    observed = []

    async def handler(method, params):
        context = trace.get_current_span().get_span_context()
        observed.append(context)
        return "ok"

    async with peers(handler, handler) as (left, right):
        left.tracing = make("caller", sampler=ParentBased(ALWAYS_OFF))
        right.tracing = make("worker")
        assert await left.request("read") == "ok"
    assert observed[0].is_valid and not observed[0].trace_flags.sampled
    assert not exporter.get_finished_spans()


async def test_bad_or_missing_carrier_does_not_inherit_reader_context(telemetry):
    make, exporter = telemetry
    tracing = make("worker")
    for carrier in ({}, {"traceparent": 42}, {"traceparent": "x" * 10000}, ["invalid"]):
        with (
            tracing.span("unrelated") as unrelated,
            tracing.span("incoming", kind="server", carrier=carrier) as incoming,
        ):
            assert (
                incoming.get_span_context().trace_id
                != unrelated.get_span_context().trace_id
            )
    assert (
        len([span for span in exporter.get_finished_spans() if span.parent is None])
        == 8
    )


async def test_background_task_starts_a_new_trace(telemetry):
    make, exporter = telemetry
    tracing = make("worker")

    class Peer:
        async def request(self, method, params):
            return {"root_id": "task-root"}

    peer = Peer()
    peer.tracing = tracing
    context = RuntimeContext(peer, plugin_id="example", generation="g1")
    context.ready.set()

    async def work():
        return trace.get_current_span().get_span_context().trace_id

    with tracing.span("activation") as parent:
        task = context.task(work, name="refresh")
        identifier = await task
        assert identifier != parent.get_span_context().trace_id
    await context.close()
    assert {span.name for span in exporter.get_finished_spans()} == {
        "activation",
        "plugin.task",
    }


async def test_disabled_rpc_keeps_the_original_wire_format():
    sent = []

    async def handler(method, params):
        return params

    async with peers(handler, handler) as (left, _):
        original = left.send

        async def observe(message):
            sent.append(json.loads(json.dumps(message)))
            await original(message)

        left.send = observe
        assert await left.request("read", {"value": 1}) == {"value": 1}
    assert sent == [
        {"jsonrpc": "2.0", "id": "1", "method": "read", "params": {"value": 1}}
    ]


def test_enabled_runtime_rejects_unsupported_protocol_without_starting_threads(
    monkeypatch,
):
    monkeypatch.setenv("TOOLFRAME_TRACING_ENABLED", "true")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://localhost:4318/v1/traces"
    )
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "grpc")
    with pytest.raises(PluginError, match="http/protobuf"):
        configure_tracing("example")


async def test_unavailable_collector_does_not_fail_calls_or_block_shutdown(monkeypatch):
    for key in os.environ:
        if key.startswith("OTEL_"):
            monkeypatch.delenv(key)
    # Reserve a local port without listening: every export is refused.
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        monkeypatch.setenv("TOOLFRAME_TRACING_ENABLED", "true")
        monkeypatch.setenv(
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
            f"http://127.0.0.1:{unavailable.getsockname()[1]}/v1/traces",
        )
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT", "0.05")
        monkeypatch.setenv("OTEL_BSP_SCHEDULE_DELAY", "1")
        tracing = configure_tracing("unavailable-collector")

        async def handler(method, params):
            return params

        try:
            async with peers(handler, handler) as (left, right):
                left.tracing = right.tracing = tracing
                assert await left.request("echo", {"value": 42}) == {"value": 42}
                await asyncio.sleep(0.02)
                assert await left.request("echo", {"value": 43}) == {"value": 43}
        finally:
            async with asyncio.timeout(3):
                await asyncio.to_thread(tracing.shutdown)
