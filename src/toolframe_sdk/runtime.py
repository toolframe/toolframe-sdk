"""Plugin-side context. Identity and service authorization are enforced by core."""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import logging
import time
import uuid
from contextlib import asynccontextmanager

from .contracts import OwnedResource, PluginError

call_context: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "toolframe_call", default=None
)


class RuntimeContext:
    def __init__(
        self, peer, *, plugin_id, generation, config=None, state_directory=None
    ):
        self.peer, self.plugin_id, self.generation = peer, plugin_id, generation
        self.config = config or {}
        self.state_directory = state_directory
        self.tasks: set[asyncio.Task] = set()
        self.ready = asyncio.Event()
        self.task_errors: list[str] = []

    @property
    def call_info(self) -> dict:
        return call_context.get() or {}

    async def call(self, alias, method, params=None, *, timeout=30):
        info = self.call_info
        remaining = min(
            timeout, info.get("deadline", time.time() + timeout) - time.time()
        )
        if remaining <= 0:
            raise PluginError("deadline_exceeded", "Call deadline exceeded")
        return await self.peer.request(
            "service.call",
            {
                "alias": alias,
                "method": method,
                "params": params or {},
                "root_id": info.get("root_id"),
                "chain": info.get("chain", []),
            },
            timeout=remaining,
        )

    async def resource_call(self, key, method, params=None, *, epoch, timeout=30):
        return await self.peer.request(
            "resource.call",
            {
                "key": key,
                "method": method,
                "params": params or {},
                "epoch": epoch,
            },
            timeout=timeout,
        )

    async def manage(self, action, params=None):
        return await self.peer.request(
            "management.call", {"action": action, "params": params or {}}
        )

    def storage(self, alias="storage"):
        return StorageClient(self, alias)

    def task(self, factory, *, name=None):
        async def run():
            await self.ready.wait()
            root = await self.peer.request("task.begin", {})
            token = call_context.set(root)
            try:
                return await factory()
            except Exception as error:
                self.task_errors.append(str(error))
                raise
            finally:
                call_context.reset(token)
                await self.peer.request("task.end", {"root_id": root["root_id"]})

        task = asyncio.create_task(run(), name=name)
        self.tasks.add(task)

        def finished(task):
            self.tasks.discard(task)
            if not task.cancelled():
                task.exception()  # retrieve after retaining it for health diagnostics

        task.add_done_callback(finished)
        return task

    async def close(self):
        self.ready.clear()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class StorageClient:
    def __init__(self, context, alias="storage", token=None):
        self.context, self.alias, self.token = context, alias, token

    async def query(self, sql, parameters=()):
        return await self.context.call(
            self.alias,
            "query",
            {
                "sql": sql,
                "parameters": list(parameters),
                "transaction": self.token,
            },
        )

    async def execute(self, sql, parameters=(), *, mutation_id=None):
        return await self.context.call(
            self.alias,
            "execute",
            {
                "sql": sql,
                "parameters": list(parameters),
                "transaction": self.token,
                "mutation_id": mutation_id or uuid.uuid4().hex,
            },
        )

    async def receipt(self, mutation_id):
        return await self.context.call(
            self.alias, "receipt", {"mutation_id": mutation_id}
        )

    @asynccontextmanager
    async def transaction(self, *, mutation_id=None):
        if self.token:
            raise PluginError(
                "nested_transaction", "Nested transactions are not supported"
            )
        mutation_id = mutation_id or uuid.uuid4().hex
        result = await self.context.call(
            self.alias, "begin", {"mutation_id": mutation_id}
        )
        if result.get("committed"):
            raise PluginError("already_committed", "Mutation already committed", result)
        client = StorageClient(self.context, self.alias, result["transaction"])
        try:
            yield client
        except BaseException:
            try:
                await asyncio.shield(
                    self.context.call(
                        self.alias, "rollback", {"transaction": client.token}
                    )
                )
            except Exception:  # noqa: BLE001 - preserve the original business failure on disconnect
                logging.getLogger(__name__).warning(
                    "Rollback unacknowledged; provider root finalization must settle it"
                )
            raise
        else:
            await self.context.call(self.alias, "commit", {"transaction": client.token})


class Plugin:
    def __init__(self, context: RuntimeContext):
        self.context = context

    async def activate(self):
        pass

    async def health(self):
        return {"runtime": "ready", "business": "not_provided"}

    async def invoke(self, service: str, method: str, params: dict):
        raise PluginError("method_not_found", f"Unknown method {service}/{method}")

    async def quiesce(self):
        self.context.ready.clear()

    async def resume(self):
        self.context.ready.set()

    async def dispose(self):
        await self.context.close()

    async def prepare(self, resources: list[OwnedResource], epoch: str):
        return {"epoch": epoch}

    async def commit(self, epoch: str):
        return {"epoch": epoch}

    async def abort(self, epoch: str):
        return {"epoch": epoch}


async def resolve(value):
    return await value if inspect.isawaitable(value) else value
