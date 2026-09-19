"""Private worker executable; stdout belongs exclusively to JSON-RPC."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.resources
import os
import sys

from .contracts import (
    MAX_FRAME_BYTES,
    OwnedResource,
    PluginDefinition,
    PluginError,
    load_object,
    validate,
)
from .rpc import RpcPeer
from .runtime import RuntimeContext, call_context, resolve


class Worker:
    def __init__(self, entrypoint):
        self.entrypoint, self.definition, self.plugin, self.context = (
            entrypoint,
            None,
            None,
            None,
        )
        self.peer = None
        self.lifecycle_lock = asyncio.Lock()

    def describe(self):
        if self.definition is None:
            value = load_object(self.entrypoint)
            if not isinstance(value, PluginDefinition):
                raise PluginError(
                    "invalid_definition", "Entry point must export a PluginDefinition"
                )
            self.definition = value
        documents = {}
        for resource in self.definition.resources:
            if resource.kind == "llms.document":
                package = importlib.resources.files(resource.data["package"])
                text = package.joinpath(resource.data["path"]).read_text(
                    encoding="utf-8"
                )
                if len(text.encode()) > 256 * 1024:
                    raise PluginError("document_too_large", "Document exceeds 256 KiB")
                documents[resource.id] = text
        return {
            "protocol": 1,
            "definition": self.definition.model_dump(mode="json"),
            "documents": documents,
        }

    async def handle(self, method, params):
        if method == "runtime.describe":
            return self.describe()
        if method == "runtime.activate":
            async with self.lifecycle_lock:
                if self.plugin:
                    raise PluginError("already_active", "Worker has already activated")
                self.describe()
                validate(self.definition.config_schema, params.get("config", {}))
                self.context = RuntimeContext(
                    self.peer,
                    plugin_id=self.definition.id,
                    generation=params["generation"],
                    config=params.get("config"),
                    state_directory=params.get("state_directory"),
                )
                factory = load_object(self.definition.factory)
                self.plugin = await resolve(factory(self.context))
                token = call_context.set(params.get("context", {}))
                try:
                    await self.plugin.activate()
                    return await self.plugin.health()
                finally:
                    call_context.reset(token)
        if not self.plugin:
            raise PluginError("not_active", "Worker is not active")
        if method == "runtime.invoke":
            service, name, payload = (
                params["service"],
                params["method"],
                params.get("params", {}),
            )
            specification = self.definition.services.get(service)
            if specification is None or name not in specification.methods:
                raise PluginError(
                    "method_not_found", f"Unknown method {service}/{name}"
                )
            method_spec = specification.methods[name]
            validate(method_spec.input_schema, payload)
            token = call_context.set(params.get("context", {}))
            try:
                result = await self.plugin.invoke(service, name, payload)
                validate(method_spec.output_schema, result)
                return result
            finally:
                call_context.reset(token)
        if method == "runtime.health":
            token = call_context.set(params.get("context", {}))
            try:
                health = await self.plugin.health()
            finally:
                call_context.reset(token)
            if self.context.task_errors:
                return {"runtime": "failed", "errors": list(self.context.task_errors)}
            return health
        async with self.lifecycle_lock:
            if method == "runtime.quiesce":
                await self.plugin.quiesce()
            elif method == "runtime.resume":
                await self.plugin.resume()
            elif method == "runtime.dispose":
                try:
                    await self.plugin.dispose()
                finally:
                    await self.context.close()
                self.plugin = None
            elif method == "provider.prepare":
                return await self.plugin.prepare(
                    [OwnedResource.model_validate(x) for x in params["resources"]],
                    params["epoch"],
                )
            elif method == "provider.commit":
                return await self.plugin.commit(params["epoch"])
            elif method == "provider.abort":
                return await self.plugin.abort(params["epoch"])
            else:
                raise PluginError(
                    "method_not_found", f"Unknown control method {method}"
                )
        return {}


async def run(entrypoint):
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_FRAME_BYTES)
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
    )
    # Duplicate the protocol FD, then send accidental print()/library stdout to stderr.
    protocol_output = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    transport, protocol = await loop.connect_write_pipe(
        lambda: asyncio.streams.FlowControlMixin(loop=loop), protocol_output
    )
    writer = asyncio.StreamWriter(transport, protocol, None, loop)
    worker = Worker(entrypoint)
    peer = RpcPeer(reader, writer, worker.handle)
    worker.peer = peer
    try:
        await peer.run()
    finally:
        # Losing the parent is terminal: dispose without accepting another owner.
        if worker.plugin:
            with contextlib.suppress(Exception):
                async with asyncio.timeout(10):
                    await worker.plugin.dispose()
        if worker.context:
            await worker.context.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--entrypoint", required=True)
    parser.add_argument("--host-token", help=argparse.SUPPRESS)
    args = parser.parse_args()
    asyncio.run(run(args.entrypoint))


if __name__ == "__main__":
    main()
