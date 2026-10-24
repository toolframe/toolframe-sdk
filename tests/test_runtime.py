import asyncio
import time

import pytest

from toolframe_sdk import Plugin, PluginError, RuntimeContext, call_context


class FakePeer:
    """Small task protocol peer with controllable admission/finalization."""

    def __init__(
        self,
        *,
        deadline=None,
        begin_gate=None,
        begin_script=(),
        end_gate=None,
        end_error=None,
    ):
        self.deadline = deadline
        self.begin_gate = begin_gate
        self.begin_script = list(begin_script)
        self.end_gate = end_gate
        self.end_error = end_error
        self.calls = []
        self.begin_started = asyncio.Event()
        self.end_started = asyncio.Event()
        self._root_number = 0

    @property
    def begin_calls(self):
        return [params for method, params in self.calls if method == "task.begin"]

    @property
    def end_calls(self):
        return [params for method, params in self.calls if method == "task.end"]

    async def request(self, method, params=None, **_kwargs):
        params = {} if params is None else params
        self.calls.append((method, params))
        if method == "task.begin":
            self.begin_started.set()
            if self.begin_gate is not None:
                await self.begin_gate.wait()
            if self.begin_script:
                result = self.begin_script.pop(0)
                if isinstance(result, BaseException):
                    raise result
                return result
            self._root_number += 1
            return {
                "root_id": f"root-{self._root_number}",
                "deadline": (
                    self.deadline if self.deadline is not None else time.time() + 30
                ),
            }
        if method == "task.end":
            self.end_started.set()
            if self.end_gate is not None:
                await self.end_gate.wait()
            if self.end_error is not None:
                raise self.end_error
            return {}
        raise AssertionError(f"unexpected peer request: {method}")


def context(peer):
    runtime = RuntimeContext(peer, plugin_id="example", generation="g1")
    runtime.ready.set()
    return runtime


@pytest.mark.parametrize("phase", ["complete", "authorize"])
async def test_resource_admission_transports_phase_without_granting_local_authority(
    phase,
):
    class Peer:
        async def request(self, method, params):
            assert method == "resource.admit"
            assert params["phase"] == phase
            assert params["access_context"] == "opaque"
            return {"authorized": True}

    runtime = context(Peer())
    options = {} if phase == "complete" else {"phase": phase}
    assert await runtime.resource_admit(
        "example:main", "save", epoch="e", access_context="opaque", **options
    ) == {"authorized": True}


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        True,
        False,
        "5",
        object(),
        -1,
        0,
        10**10000,
    ],
    ids=[
        "nan",
        "positive-infinity",
        "negative-infinity",
        "true",
        "false",
        "string",
        "object",
        "negative",
        "zero",
        "huge-integer",
    ],
)
def test_invalid_timeout_is_rejected_before_admission(value):
    peer = FakePeer()
    runtime = context(peer)

    async def factory():
        raise AssertionError("invalid timeout must not run the factory")

    with pytest.raises(PluginError) as error:
        runtime.task(factory, timeout=value)

    assert error.value.code == "invalid_task_timeout"
    assert peer.calls == []
    assert runtime.tasks == set()


async def test_omitted_timeout_keeps_empty_begin_payload_and_legacy_factory_behavior():
    peer = FakePeer(deadline=time.time() - 1)
    runtime = context(peer)
    started = asyncio.Event()
    release = asyncio.Event()
    observed_context = []

    async def factory():
        observed_context.append(call_context.get())
        started.set()
        await release.wait()
        return "complete"

    managed = runtime.task(factory)
    await asyncio.wait_for(started.wait(), 1)
    await asyncio.sleep(0.02)
    assert not managed.done()
    assert peer.begin_calls == [{}]
    assert observed_context[0]["root_id"] == "root-1"

    release.set()
    assert await asyncio.wait_for(managed, 1) == "complete"
    assert peer.end_calls == [{"root_id": "root-1"}]
    assert runtime.tasks == set()
    assert call_context.get() is None


async def test_core_rejects_budget_above_policy_without_admitting_or_ending_root():
    peer = FakePeer(
        begin_script=[
            PluginError(
                "task_timeout_exceeded",
                "Task timeout exceeds the configured maximum",
                {"max_task_timeout": 3600},
            )
        ]
    )
    runtime = context(peer)
    factory_called = False

    async def factory():
        nonlocal factory_called
        factory_called = True

    managed = runtime.task(factory, timeout=3601)
    with pytest.raises(PluginError) as error:
        await managed

    assert error.value.code == "task_timeout_exceeded"
    assert peer.begin_calls == [{"timeout": 3601.0}]
    assert peer.end_calls == []
    assert not factory_called
    assert runtime.tasks == set()


async def test_explicit_timeout_uses_authoritative_earlier_deadline_and_cleans_up():
    peer = FakePeer(deadline=time.time() + 0.04)
    runtime = context(peer)
    started = asyncio.Event()
    observed_context = []

    async def factory():
        observed_context.append(call_context.get())
        started.set()
        await asyncio.Event().wait()

    managed = runtime.task(factory, timeout=5)
    begin = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(managed, 0.5)
    elapsed = time.monotonic() - begin

    assert elapsed < 0.25
    assert peer.begin_calls == [{"timeout": 5.0}]
    assert peer.end_calls == [{"root_id": "root-1"}]
    assert observed_context[0]["root_id"] == "root-1"
    assert runtime.tasks == set()
    assert call_context.get() is None


async def test_factory_failure_is_recorded_and_still_ends_admitted_root():
    peer = FakePeer()
    runtime = context(peer)

    async def factory():
        raise ValueError("factory failed")

    managed = runtime.task(factory)
    with pytest.raises(ValueError, match="factory failed"):
        await managed

    assert runtime.task_errors == ["factory failed"]
    assert peer.end_calls == [{"root_id": "root-1"}]
    assert runtime.tasks == set()
    assert call_context.get() is None


async def test_cancellation_during_factory_preserves_cancellation_and_cleans_up():
    peer = FakePeer()
    runtime = context(peer)
    started = asyncio.Event()

    async def factory():
        started.set()
        await asyncio.Event().wait()

    managed = runtime.task(factory)
    await asyncio.wait_for(started.wait(), 1)
    managed.cancel()

    with pytest.raises(asyncio.CancelledError):
        await managed

    assert peer.end_calls == [{"root_id": "root-1"}]
    assert runtime.tasks == set()
    assert call_context.get() is None


async def test_cancellation_during_admission_waits_for_root_then_ends_it():
    begin_gate = asyncio.Event()
    peer = FakePeer(begin_gate=begin_gate)
    runtime = context(peer)

    async def factory():
        raise AssertionError("cancelled admission must not run the factory")

    managed = runtime.task(factory)
    await asyncio.wait_for(peer.begin_started.wait(), 1)
    managed.cancel()
    await asyncio.sleep(0)
    assert not peer.end_started.is_set()

    begin_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(managed, 1)

    assert peer.end_calls == [{"root_id": "root-1"}]
    assert runtime.tasks == set()


async def test_repeated_cancellation_during_blocked_cleanup_waits_for_end():
    end_gate = asyncio.Event()
    peer = FakePeer(end_gate=end_gate)
    runtime = context(peer)
    started = asyncio.Event()

    async def factory():
        started.set()
        await asyncio.Event().wait()

    managed = runtime.task(factory)
    await asyncio.wait_for(started.wait(), 1)
    managed.cancel()
    await asyncio.wait_for(peer.end_started.wait(), 1)

    managed.cancel()
    managed.cancel()
    await asyncio.sleep(0.02)
    assert not managed.done()

    end_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(managed, 1)
    assert peer.end_calls == [{"root_id": "root-1"}]
    assert runtime.tasks == set()


async def test_cleanup_failure_does_not_replace_factory_cancellation():
    peer = FakePeer(end_error=RuntimeError("end failed"))
    runtime = context(peer)
    started = asyncio.Event()

    async def factory():
        started.set()
        await asyncio.Event().wait()

    managed = runtime.task(factory)
    await asyncio.wait_for(started.wait(), 1)
    managed.cancel()

    with pytest.raises(asyncio.CancelledError):
        await managed

    assert peer.end_calls == [{"root_id": "root-1"}]
    assert runtime.task_errors == ["end failed"]
    assert runtime.tasks == set()


async def test_task_waits_while_quiesced_and_admits_only_after_resume():
    peer = FakePeer()
    runtime = RuntimeContext(peer, plugin_id="example", generation="g1")
    plugin = Plugin(runtime)
    await plugin.quiesce()
    started = asyncio.Event()

    async def factory():
        started.set()
        return "ready"

    managed = runtime.task(factory)
    await asyncio.sleep(0.02)
    assert peer.calls == []
    assert not started.is_set()

    await plugin.resume()
    assert await asyncio.wait_for(managed, 1) == "ready"
    assert started.is_set()
    assert peer.end_calls == [{"root_id": "root-1"}]


async def test_stale_admission_does_not_leak_and_next_request_can_succeed():
    peer = FakePeer(
        begin_script=[
            PluginError("stale_generation", "Worker generation is no longer current"),
            {"root_id": "root-valid", "deadline": time.time() + 30},
        ]
    )
    runtime = context(peer)
    factory_called = []

    async def stale_factory():
        factory_called.append("stale")

    first = runtime.task(stale_factory)
    with pytest.raises(PluginError) as error:
        await first
    assert error.value.code == "stale_generation"
    assert peer.end_calls == []
    assert runtime.tasks == set()
    assert factory_called == []

    async def valid_factory():
        factory_called.append("valid")
        return "ok"

    second = runtime.task(valid_factory)
    assert await second == "ok"
    assert factory_called == ["valid"]
    assert peer.begin_calls == [{}, {}]
    assert peer.end_calls == [{"root_id": "root-valid"}]
    assert runtime.tasks == set()


async def test_expired_authoritative_deadline_rejects_factory_without_suspension():
    peer = FakePeer(deadline=time.time() - 1)
    runtime = context(peer)
    called = False

    async def factory():
        nonlocal called
        called = True
        return "must not run"

    with pytest.raises(TimeoutError):
        await runtime.task(factory, timeout=5)
    assert not called
    assert peer.end_calls == [{"root_id": "root-1"}]
    assert not runtime.tasks
    assert call_context.get() is None
    assert runtime.task_errors == ["TimeoutError"]


async def test_cancellation_during_failed_cleanup_preserves_cancellation_once():
    end_gate = asyncio.Event()
    peer = FakePeer(end_gate=end_gate, end_error=RuntimeError("end failed"))
    runtime = context(peer)

    async def factory():
        return "factory completed"

    managed = runtime.task(factory, timeout=5)
    await asyncio.wait_for(peer.end_started.wait(), 1)
    managed.cancel()
    await asyncio.sleep(0)
    assert not managed.done()
    end_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await managed
    assert peer.end_calls == [{"root_id": "root-1"}]
    assert runtime.task_errors == ["end failed"]
    assert not runtime.tasks
    assert call_context.get() is None
