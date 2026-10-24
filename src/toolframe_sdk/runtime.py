"""Plugin-side context. Identity and service authorization are enforced by core."""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import logging
import math
import sys
import time
import uuid
from contextlib import asynccontextmanager

from .contracts import OwnedResource, PluginError
from .logging import log_context, plugin_logger

call_context: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "toolframe_call", default=None
)


class RuntimeContext:
    def __init__(
        self,
        peer,
        *,
        plugin_id,
        generation,
        config=None,
        state_directory=None,
        private_secrets=None,
        endpoints=None,
    ):
        self.peer, self.plugin_id, self.generation = peer, plugin_id, generation
        self.config = config or {}
        self.state_directory = state_directory
        self.private_secrets = private_secrets or {}
        self.endpoints = endpoints or {}
        tracing = getattr(peer, "tracing", None)
        level = (
            min(logging.INFO, tracing.logs.handler.level)
            if tracing and tracing.logs
            else logging.INFO
        )
        self.log = plugin_logger(plugin_id, generation, level=level)
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

    async def resource_admit(
        self, key, route, *, epoch, access_context, csrf="", revision="", asset=None
    ):
        return await self.peer.request(
            "resource.admit",
            {
                "key": key,
                "route": route,
                "epoch": epoch,
                "access_context": access_context,
                "csrf": csrf,
                "revision": revision,
                "asset": asset,
            },
        )

    async def resource_call(
        self,
        key,
        method,
        params=None,
        *,
        epoch,
        timeout=30,
        access_context=None,
        activity=True,
    ):
        return await self.peer.request(
            "resource.call",
            {
                "key": key,
                "method": method,
                "params": params or {},
                "epoch": epoch,
                "access_context": access_context,
                "activity": activity,
            },
            timeout=timeout,
        )

    async def manage(self, action, params=None, *, access_context=None):
        return await self.peer.request(
            "management.call",
            {
                "action": action,
                "params": params or {},
                "access_context": access_context,
            },
        )

    async def access(self, action, params=None, *, access_context=None):
        return await self.peer.request(
            "access.call",
            {
                "action": action,
                "params": params or {},
                "access_context": access_context,
            },
        )

    async def mcp_access(self, access_context):
        """Resolve a core-issued MCP request credential; MCP providers only."""
        return await self.peer.request(
            "mcp.access", {"access_context": access_context}, timeout=30
        )

    def object_store(self, alias="objects"):
        from .object_store import ObjectStoreClient

        return ObjectStoreClient(self, alias)

    def sql(self, alias="sql"):
        return SqlClient(self, alias)

    def task(self, factory, *, name=None, timeout=None):
        """Start a managed attempt; an explicit budget bounds the entire factory."""
        if timeout is not None:
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
                raise PluginError(
                    "invalid_task_timeout", "Task timeout must be finite and positive"
                )
            try:
                timeout = float(timeout)
            except OverflowError as error:
                raise PluginError(
                    "invalid_task_timeout", "Task timeout must be finite and positive"
                ) from error
            if not math.isfinite(timeout) or timeout <= 0:
                raise PluginError(
                    "invalid_task_timeout", "Task timeout must be finite and positive"
                )

        async def settle(awaitable):
            # Finish the ownership handshake even if disposal cancels us again.
            operation = asyncio.ensure_future(awaitable)
            cancellation = None
            while True:
                try:
                    return await asyncio.shield(operation), cancellation
                except asyncio.CancelledError as error:
                    if operation.cancelled():
                        raise
                    cancellation = error
                except Exception as error:
                    if cancellation is not None:
                        self.task_errors.append(str(error) or type(error).__name__)
                        raise cancellation from None
                    raise

        async def run():
            await self.ready.wait()
            with log_context(
                {
                    "toolframe.operation": "plugin.task",
                    "toolframe.task.name": name or "background",
                },
                replace=True,
            ):
                tracing = getattr(self.peer, "tracing", None)
                if tracing:
                    with tracing.span(
                        "plugin.task",
                        carrier={},
                        attributes={"toolframe.task.name": name or "background"},
                    ):
                        return await admitted()
                return await admitted()

        async def admitted():
            root, token = None, None
            admission = asyncio.create_task(
                self.peer.request(
                    "task.begin", {} if timeout is None else {"timeout": timeout}
                )
            )
            try:
                try:
                    root = await asyncio.shield(admission)
                except asyncio.CancelledError:
                    try:
                        root, _ = await settle(admission)
                    except (Exception, asyncio.CancelledError) as error:  # noqa: BLE001 - preserve cancellation even when admission fails
                        if not isinstance(error, asyncio.CancelledError):
                            self.task_errors.append(str(error) or type(error).__name__)
                    raise
                token = call_context.set(root)
                with log_context({"toolframe.root.id": root["root_id"]}):
                    try:
                        if timeout is None:
                            return await factory()
                        remaining = root["deadline"] - time.time()
                        if remaining <= 0:
                            raise TimeoutError
                        async with asyncio.timeout(remaining):
                            return await factory()
                    except Exception as error:
                        self.task_errors.append(str(error) or type(error).__name__)
                        self.log.error(
                            "Plugin task failed",
                            extra={"error.type": type(error).__name__},
                        )
                        raise
            finally:
                failure = sys.exception()
                if token is not None:
                    call_context.reset(token)
                if root is not None:
                    try:
                        _, cancellation = await settle(
                            self.peer.request("task.end", {"root_id": root["root_id"]})
                        )
                    except Exception as error:
                        self.task_errors.append(str(error) or type(error).__name__)
                        if isinstance(failure, asyncio.CancelledError):
                            raise failure
                        raise
                    if cancellation is not None:
                        raise cancellation

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


class SqlClient:
    def __init__(self, context, alias="sql", token=None):
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

    async def execute_returning(self, sql, parameters=(), *, mutation_id=None):
        """Execute a mutation with RETURNING, preserving transaction/receipt identity."""
        return await self.context.call(
            self.alias,
            "execute_returning",
            {
                "sql": sql,
                "parameters": list(parameters),
                "transaction": self.token,
                "mutation_id": mutation_id or uuid.uuid4().hex,
            },
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
        client = SqlClient(self.context, self.alias, result["transaction"])
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
                self.context.log.warning(
                    "Rollback unacknowledged; provider root finalization must settle it"
                )
            raise
        else:
            await self.context.call(self.alias, "commit", {"transaction": client.token})


class Plugin:
    def __init__(self, context: RuntimeContext):
        self.context = context
        self.log = context.log

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
