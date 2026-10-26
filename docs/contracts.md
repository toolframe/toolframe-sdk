# Plugin definitions and worker contracts

One wheel exports one `toolframe.plugins` entry point whose value is a
`PluginDefinition`. Its factory creates a `Plugin` with a `RuntimeContext`.
Services exchange validated JSON; page/tool/document declarations are owned by
their business package. Activation and resource publication are separate phases.

## Authorization and publication

SDK 0.2 requires `authorization=AuthorizationSpec(...)` on every definition,
including an empty declaration on infrastructure providers. Business scopes are
namespaced by plugin ID (`notes.read`, `notes.write`). Every `MethodSpec` declares
`audience="business"|"system"|"internal"`; business/system methods require matching
scopes. Internal services are available only through admitted plugin dependencies.
Legacy definitions without this declaration fail validation.

Dashboard resources declare `required_scopes`. `PageContext.can(scope)` is for UI
controls; core separately enforces all method requirements using its trusted
browser context. `service.call(..., activity=False)` marks polling so it does not
renew sessions or update last use. Nested calls retain the root's admitted actor
and scope catalog. Do not construct browser principals in plugin code.

`public_api=True` explicitly publishes a business method through core's public
HTTP API. MCP tool declarations also publish their business target. Neither uses
Dashboard password scopes; internal/system methods cannot be public. `read_only`
describes side effects, not publication or permission.

MCP uses core's independent public/protected access policy. The `mcp-auth`
provider kind supplies internal credential decisions; an `mcp_key` principal has
fixed business scopes and no implicit operator authority. `system.auth.mcp_keys`
controls administration. MCP providers resolve core-issued request credentials
with `RuntimeContext.mcp_access(access_context)` and pass the same reference to
`resource_call(..., access_context=...)`. Core validates it again at tool admission;
providers must never construct caller principals or accept client-supplied context.

The optional `oidc-auth` provider kind supplies shared authentication adapters.
`Principal(kind="user")` identifies an admitted local user through `user_id`;
`kind="delegated"` binds business scopes to `user_id`, `client_id`, `grant_id`,
`resource` and expiry. Neither kind has implicit operator authority. Core checks
these bindings at admission. `system.auth.users` controls user administration;
`system.auth.self` controls the current user's connections. Business plugins keep
declaring scopes normally and do not validate OIDC tokens themselves.

## Private secrets

Plugins may declare stable `private_secrets` names. Core delivers their retained
keys through `RuntimeContext.private_secrets`, independently of configuration and
the database. Keys do not belong in descriptors, config, health, logs or output.

## Web and background work

For full HTML documents, forms, fragments and app-owned browser assets, see
[Web contract v1](../docs/web.md). Web handlers use request-scoped business calls and
work independently of the existing Dashboard page bridge.

For bounded plugin background work, see [managed attempts and polling](../docs/tasks.md).

## Worker protocol and diagnostics

Workers run with `python -m toolframe_sdk.worker --entrypoint package.module:plugin`.
Stdin/stdout carry bounded JSON-RPC 2.0. [Plugin logging](../docs/logging.md) uses
`context.log` (or `self.log` on `Plugin`) with automatic plugin identity and
function/call context. Structured local records go to stderr; accidental library
stdout is redirected there to protect the protocol.
The reader dispatches concurrently so a service handler can await a nested host
call. A lost control pipe closes the worker and its managed resources.


Binary images and attachments use `RuntimeContext.object_store()` and the internal
`toolframe.object_store` v1 service. See [object storage](../docs/object-storage.md)
for chunked transfers, ownership, limits and provider authoring.

Optional [runtime tracing](../docs/tracing.md) connects HTTP and nested plugin calls
through W3C trace context. It is disabled by default and uses the `tracing` extra
only when explicitly enabled by the host.
Log export is separately opt-in through `OTEL_LOGS_EXPORTER=otlp`.
