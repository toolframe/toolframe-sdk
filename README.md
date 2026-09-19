# Toolframe SDK

Public package definitions, service/resource contracts, scoped storage clients and
the isolated Python worker protocol. Core and providers depend on this package;
it imports neither core nor Streamlit/FastMCP.

One wheel exports one `toolframe.plugins` entry point whose value is a
`PluginDefinition`. Its factory creates a `Plugin` with a `RuntimeContext`.
Services exchange validated JSON; page/tool/document declarations are owned by
their business package. Activation and resource publication are separate phases.

Workers run with `python -m toolframe_sdk.worker --entrypoint package.module:plugin`.
Stdin/stdout carry bounded JSON-RPC 2.0; logs and ordinary prints go to stderr.
The reader dispatches concurrently so a service handler can await a nested host
call. A lost control pipe closes the worker and its managed resources.

Install development dependencies with `uv sync --group dev`, then run `uv run pytest`.
