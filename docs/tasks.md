# Managed background attempts

Use `RuntimeContext.task(factory, *, name=None, timeout=None)` for a single
background operation that calls declared plugin dependencies. `factory` is a
zero-argument callable returning an awaitable. The method returns an
`asyncio.Task`; await it to observe its result, `TimeoutError`, business failure
or cancellation.

```python
attempt = context.task(process_next_job, name="next-job", timeout=120)
result = await attempt
```

An explicit timeout is a finite positive number of seconds; booleans and other
types are rejected with `PluginError(code="invalid_task_timeout")`. Core checks
the budget again and rejects values above its operator policy with
`task_timeout_exceeded`. It returns the authoritative root deadline after
admission; the SDK bounds the whole factory using that deadline, including local
awaits and nested calls. Normal cooperative asyncio cancellation applies. A
factory must not suppress cancellation or block the event loop.

Admission waits until the plugin is ready and Core reopens admission after
quiescence. Waiting does not consume the requested attempt budget or acquire a
dependency lease. On admission, the root retains the current dependency
generations until `task.end` has settled their cleanup. Cleanup is outside the
factory timeout. Cancellation waits for an in-flight admission response and for
root cleanup, including repeated cancellation during disposal; it remains
cancellation after cleanup. A disconnected control channel can prevent cleanup
acknowledgement, which Core must resolve through its normal ownership recovery.

Omitting `timeout`, or passing `None`, keeps the existing call shape and behavior:
Core grants its default 30-second root deadline, nested service calls respect
that deadline, and the SDK does not add an attempt-wide timeout around the
factory. Use an explicit timeout for every bounded attempt. A dependency call
has its own default 30-second request timeout; pass `timeout=` to `context.call`
when it needs a larger share of the admitted budget.

## Plugin-owned polling

A polling loop belongs to the plugin lifecycle. Keep the loop outside
`context.task`; create a separate bounded task for each attempt so idle polling
never retains an admitted root. Quiesce must set a stop signal before returning,
prevent new attempts and let admitted attempts drain. Resume clears that signal
and restarts or resumes at most one poller. Dispose closes managed tasks and
stops the remaining poller. Do not register an endless scheduler as one managed
task.

The scheduling portion can look like this; the plugin's lifecycle hooks own the
`stop` event and the poller task:

```python
async def poll(context, stop, process_next_job):
    while not stop.is_set():
        await context.ready.wait()
        if stop.is_set():
            break
        await context.task(process_next_job, timeout=120)
        try:
            await asyncio.wait_for(stop.wait(), timeout=10)
        except TimeoutError:
            pass
```

Quiesce clears readiness through `await super().quiesce()` and sets `stop`;
it must not cancel admitted attempts merely to stop polling. A failed attempt
remains visible in worker health diagnostics. Cancellation does not become a
business failure. The existing `call_info` carries Core-issued root and caller
information; a poller does not manufacture another actor or bypass declared
service requirements.
