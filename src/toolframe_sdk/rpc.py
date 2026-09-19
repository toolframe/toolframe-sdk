"""Bounded, concurrent bidirectional JSON-RPC; the reader never awaits handlers."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from .contracts import MAX_FRAME_BYTES, PluginError


class RpcPeer:
    def __init__(
        self,
        reader,
        writer,
        handler: Callable[[str, dict], Awaitable[Any]],
        *,
        max_frame_bytes: int = MAX_FRAME_BYTES,
    ):
        self.reader, self.writer, self.handler = reader, writer, handler
        self.max_frame_bytes = max_frame_bytes
        self.pending: dict[str, asyncio.Future] = {}
        self.incoming: dict[str | int, asyncio.Task] = {}
        self.counter = 0
        self.write_lock = asyncio.Lock()
        self.closed = False

    async def send(self, message: dict):
        try:
            encoded = (
                json.dumps(message, separators=(",", ":"), allow_nan=False).encode()
                + b"\n"
            )
        except (TypeError, ValueError) as error:
            raise PluginError(
                "invalid_json", "RPC values must be finite JSON data"
            ) from error
        if len(encoded) > self.max_frame_bytes:
            raise PluginError(
                "frame_too_large", "RPC message exceeds the 1 MiB frame limit"
            )
        async with self.write_lock:
            if self.closed:
                raise PluginError("worker_disconnected", "RPC connection is closed")
            self.writer.write(encoded)
            await self.writer.drain()

    async def request(
        self, method: str, params: dict | None = None, *, timeout: float = 30
    ):
        self.counter += 1
        identifier = str(self.counter)
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        try:
            await self.send(
                {
                    "jsonrpc": "2.0",
                    "id": identifier,
                    "method": method,
                    "params": params or {},
                }
            )
            async with asyncio.timeout(timeout):
                return await future
        except TimeoutError, asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await self.send(
                    {
                        "jsonrpc": "2.0",
                        "method": "$/cancel",
                        "params": {"id": identifier},
                    }
                )
            raise
        finally:
            self.pending.pop(identifier, None)

    async def _respond(self, identifier, method, params):
        try:
            result = await self.handler(method, params)
            if identifier is not None:
                await self.send({"jsonrpc": "2.0", "id": identifier, "result": result})
        except asyncio.CancelledError:
            if identifier is not None and not self.closed:
                with contextlib.suppress(Exception):
                    await self._error(
                        identifier, PluginError("cancelled", "Request cancelled")
                    )
        except Exception as error:  # noqa: BLE001 - plugin failures must become RPC error responses
            if identifier is not None and not self.closed:
                problem = (
                    error
                    if isinstance(error, PluginError)
                    else PluginError("handler_failed", str(error))
                )
                with contextlib.suppress(Exception):
                    await self._error(identifier, problem)
        finally:
            self.incoming.pop(identifier, None)

    async def _error(self, identifier, error):
        await self.send(
            {
                "jsonrpc": "2.0",
                "id": identifier,
                "error": {
                    "code": -32000,
                    "message": error.message,
                    "data": error.as_dict(),
                },
            }
        )

    async def run(self):
        try:
            while not self.closed:
                try:
                    line = await self.reader.readline()
                except (ValueError, asyncio.LimitOverrunError) as error:
                    raise PluginError(
                        "frame_too_large", "Oversized RPC frame"
                    ) from error
                if not line:
                    break
                if len(line) > self.max_frame_bytes or not line.endswith(b"\n"):
                    raise PluginError(
                        "invalid_frame",
                        "RPC requires bounded newline-terminated frames",
                    )
                try:
                    message = json.loads(
                        line,
                        parse_constant=lambda value: (_ for _ in ()).throw(
                            ValueError(value)
                        ),
                    )
                    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                        raise ValueError("Invalid JSON-RPC envelope")
                    identifier = message.get("id")
                    if isinstance(identifier, bool) or not isinstance(
                        identifier, (str, int, type(None))
                    ):
                        raise TypeError("Invalid request ID")
                    if "method" in message:
                        method, params = message["method"], message.get("params", {})
                        if not isinstance(method, str) or not isinstance(params, dict):
                            raise ValueError("Method and object params required")
                        if method == "$/cancel":
                            task = self.incoming.get(params.get("id"))
                            if task:
                                task.cancel()
                        else:
                            if identifier is not None and identifier in self.incoming:
                                raise ValueError("Duplicate in-flight request ID")
                            if len(self.incoming) >= 256:
                                await self._error(
                                    identifier,
                                    PluginError("busy", "Too many in-flight requests"),
                                )
                                continue
                            task = asyncio.create_task(
                                self._respond(identifier, method, params)
                            )
                            # Notifications need unique bookkeeping so they cannot hide unbounded work.
                            key = (
                                identifier
                                if identifier is not None
                                else f"notification:{id(task)}"
                            )
                            self.incoming[key] = task
                            task.add_done_callback(
                                lambda _, key=key: self.incoming.pop(key, None)
                            )
                    else:
                        future = self.pending.get(str(identifier))
                        if future is None or future.done():
                            continue
                        if "error" in message:
                            data = message["error"].get("data", {})
                            future.set_exception(
                                PluginError(
                                    data.get("code", "remote_error"),
                                    data.get(
                                        "message",
                                        message["error"].get("message", "RPC error"),
                                    ),
                                    data.get("details"),
                                )
                            )
                        elif "result" in message:
                            future.set_result(message["result"])
                        else:
                            raise ValueError("Response requires result or error")
                except (ValueError, TypeError, KeyError, AttributeError) as error:
                    raise PluginError(
                        "invalid_frame", "Malformed JSON-RPC message"
                    ) from error
        finally:
            await self.close()

    async def close(self):
        if self.closed:
            return
        self.closed = True
        for future in self.pending.values():
            if not future.done():
                future.set_exception(
                    PluginError("worker_disconnected", "RPC connection closed")
                )
        tasks = [
            task
            for task in self.incoming.values()
            if task is not asyncio.current_task()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.writer.close()
        with contextlib.suppress(Exception):
            await self.writer.wait_closed()
