# Toolframe SDK

Public package definitions, service/resource contracts, scoped storage clients and
the isolated Python worker protocol. Core and providers depend on this package;
it imports neither core nor Streamlit/FastMCP.

The optional `oidc-auth` provider kind supplies shared authentication adapters.
`Principal(kind="user")` identifies an admitted local user through `user_id`;
`kind="delegated"` binds business scopes to `user_id`, `client_id`, `grant_id`,
`resource` and expiry. Neither kind has implicit operator authority. Core checks
these bindings at admission. `system.auth.users` controls user administration;
`system.auth.self` controls the current user's connections. Business plugins keep
declaring scopes normally and do not validate OIDC tokens themselves.

One wheel exports one `toolframe.plugins` entry point whose value is a
`PluginDefinition`. Its factory creates a `Plugin` with a `RuntimeContext`.
Services exchange validated JSON; page/tool/document declarations are owned by
their business package. Activation and resource publication are separate phases.

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

Plugins may declare stable `private_secrets` names. Core delivers their retained
keys through `RuntimeContext.private_secrets`, independently of configuration and
the database. Keys do not belong in descriptors, config, health, logs or output.

Workers run with `python -m toolframe_sdk.worker --entrypoint package.module:plugin`.
Stdin/stdout carry bounded JSON-RPC 2.0; logs and ordinary prints go to stderr.
The reader dispatches concurrently so a service handler can await a nested host
call. A lost control pipe closes the worker and its managed resources.


Binary images and attachments use `RuntimeContext.object_store()` and the internal
`toolframe.object_store` v1 service. See [object storage](docs/object-storage.md)
for chunked transfers, ownership, limits and provider authoring.

Install development dependencies with `uv sync --group dev`, then run `uv run pytest`.

For the isolated CI checks and manual TestPyPI/PyPI publishing setup, see
[CI and publishing](docs/ci.md). Run `bash scripts/validate.sh` to lint, test,
build and verify a fresh wheel installation locally.

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

For PostgreSQL migration variants and generated identifiers in SDK 0.3, see
[portable plugin storage](docs/storage.md).
