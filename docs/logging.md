# Plugin logging

Every `RuntimeContext` exposes `context.log`, a standard Python logger adapter.
The base `Plugin` also exposes it as `self.log`. Use it for application events:

```python
from toolframe_sdk import Plugin

class Entries(Plugin):
    async def invoke(self, service, method, params):
        result = await self.context.storage().query("SELECT id, label FROM entries")
        self.log.info(
            "Loaded %d entries", len(result),
            extra={"entry_count": len(result)},
        )
        return result
```

The logger supports the standard `debug`, `info`, `warning`, `error`, `critical`
and `exception` methods, lazy `%` formatting and `extra` fields. `exception`
includes the exception message and stack trace; applications choose what they
explicitly log. Keep private keys and credentials out of messages and fields.

## Identity and call context

Toolframe attaches plugin ID and generation automatically. Worker processes also
attach the application ID and service instance supplied by Core. These identity
fields cannot be replaced through `extra` or `bind`.

During a service invocation, records include `toolframe.service.name`,
`toolframe.method.name`, `toolframe.operation` and the admitted
`toolframe.root.id`. Labels are local to the current async call. Concurrent calls
do not share them, and `context.task(factory, name="refresh")` starts a fresh log
scope with its task name and root ID.

Python supplies the actual caller's function, module, file and line. They appear
as `code.function.name`, `code.namespace`, `code.file.path` and `code.line.number`.
With tracing enabled, records inside an active span also include its trace and
span IDs. Records outside a span remain uncorrelated; a new trace is not created
just to emit a log. Log export is independent of trace sampling.

Use a bound adapter for additional labels shared by several events:

```python
log = self.log.bind(component="repository")
log.info("Query completed", extra={"row_count": 12})
```

Binding creates a new adapter without changing the original logger. Pages use
the same interface through `page_context.log`; page records include
`toolframe.page.owner` and `toolframe.page.generation`. Their process resource
identity remains that of the Dashboard provider executing the page.

## Local output and optional export

Workers and Core write structured JSON records at `INFO` and above to stderr.
This works with telemetry disabled and requires no OpenTelemetry dependency.
Core retains each worker's bounded stderr tail through its existing diagnostics.
The worker's standard Python logging records also pass through these handlers,
subject to their own logger levels. Toolframe does not change third-party logger
levels or replace their existing handlers.

The SDK logs plugin activation at `INFO`, service call start/completion at
`DEBUG`, and operation failures at `ERROR`. Completion records include elapsed
milliseconds. Automatic records contain operation names and error categories,
not arguments, results, exception messages or SQL. Helper functions emit their
own application events through the same logger.

OTLP log export requires both `TOOLFRAME_TRACING_ENABLED=true` and
`OTEL_LOGS_EXPORTER=otlp`. It uses the existing `tracing` extra. Logs default to
`none`; setting a collector address alone does not enable them. Configure
`OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` or the shared `OTEL_EXPORTER_OTLP_ENDPOINT`.
`TOOLFRAME_LOGS_LEVEL` sets the minimum export level, defaulting to `INFO`; use
`DEBUG` to include automatic service call records. Local output stays at `INFO`.
`OTEL_SDK_DISABLED=true` disables all telemetry export.

Each process owns its logger provider, bounded batch queue and exporter. Core
instances in an embedding process have separate logger handlers and providers.
Closing a runtime removes its handlers before the best-effort exporter drain.
Collector outages do not fail plugin calls, but records may be dropped. The
Core tracing guide documents deployment configuration and queue limits.

`print()` is not the plugin logging interface. Workers redirect accidental
library stdout to stderr to protect the JSON-RPC channel; this raw text remains
local and is not sent to the OTLP log exporter. CLI result output continues to
use stdout independently of runtime logging.
