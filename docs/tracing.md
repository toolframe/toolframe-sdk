# Runtime tracing contract

Tracing is optional and disabled by default. Core configures the SDK runtime;
ordinary plugins need no instrumentation changes. Importing `toolframe_sdk` or
`toolframe_sdk.tracing` does not import OpenTelemetry. The `tracing` extra adds
the OpenTelemetry SDK and OTLP HTTP/protobuf exporter.

For standalone worker development, install `toolframe-sdk[tracing]` and set
`TOOLFRAME_TRACING_ENABLED=true` and an OTLP endpoint. For application startup,
sampling and exporter configuration, see the tracing guide in Toolframe Core's
documentation.

## Propagation

Each instrumented JSON-RPC request creates a client span and carries W3C
`traceparent` and optional `tracestate` in the envelope's optional `_meta` object:

```json
{"jsonrpc":"2.0","id":"1","method":"runtime.invoke","params":{},"_meta":{"traceparent":"00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"}}
```

The receiver creates a server span from that carrier. Nested calls inherit the
current handler's context; concurrent requests remain independent. Missing or
malformed metadata starts a fresh trace. Context is never placed in process
environment variables or invocation payloads, and baggage is not propagated.
Uninstrumented peers ignore `_meta`; when tracing is disabled, outgoing envelopes
remain unchanged. `runtime.describe` is an untraced initialization handshake.

HTTP servers created through `HttpServer` extract the same W3C headers and create
server spans. `/healthz` and WebSocket connections are excluded. Each
`RuntimeContext.task()` starts a new trace when its coroutine runs, avoiding a
long-running child of the activation span. Calls made by that task remain children
of its task span.

## Data and lifecycle

Runtime spans contain operation names, service/method names, dependency aliases,
resource keys, root IDs and plugin identity. They do not contain invocation
arguments/results, SQL, cookies, credentials, arbitrary headers, exception
messages or stack traces. Errors record their category; cancellation is marked.
Other libraries' instrumentation and plugin-created spans have their own data
policies.

Each process owns its provider, bounded export queue and background exporter.
The worker installs its provider before loading plugin code so libraries such as
FastMCP can use it. Core keeps a separate provider per host. Export failures do
not fail service calls. Shutdown performs a bounded best-effort drain; queued
spans can be lost on timeout or process termination. No collector or durable
telemetry spool is included in the SDK.

[Plugin logging](logging.md) uses the same process identity and active trace
context. Its OTLP exporter is separately opt-in; local context loggers work with
tracing disabled.

`tests/test_tracing.py` verifies propagation, concurrent nested requests, sampling,
failures, cancellation, task roots and disabled imports. The isolated wheel smoke
test verifies that a base SDK install has no OpenTelemetry dependency.
