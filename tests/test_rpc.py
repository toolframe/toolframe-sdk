import asyncio
import contextlib

import pytest

from toolframe_sdk import PluginError
from toolframe_sdk.rpc import RpcPeer


@contextlib.asynccontextmanager
async def peers(first, second):
    connected = asyncio.Future()

    async def accept(reader, writer):
        connected.set_result((reader, writer))

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    left_reader, left_writer = await asyncio.open_connection(
        "127.0.0.1", server.sockets[0].getsockname()[1]
    )
    right_reader, right_writer = await connected
    left, right = (
        RpcPeer(left_reader, left_writer, first),
        RpcPeer(right_reader, right_writer, second),
    )
    tasks = [asyncio.create_task(left.run()), asyncio.create_task(right.run())]
    try:
        yield left, right
    finally:
        await left.close()
        await right.close()
        await asyncio.gather(*tasks, return_exceptions=True)
        server.close()
        await server.wait_closed()


async def test_nested_bidirectional_requests_do_not_deadlock():
    async def local(method, params):
        return params["number"] * 2

    async def remote(method, params):
        return await right.request("nested", params) + 1

    async with peers(local, remote) as (left, right):
        result = await asyncio.gather(
            *(left.request("work", {"number": n}) for n in range(20))
        )
        assert result == [n * 2 + 1 for n in range(20)]


async def test_timeout_cancels_remote_handler():
    cancelled = asyncio.Event()

    async def handler(method, params):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async with peers(handler, handler) as (left, _):
        with pytest.raises(TimeoutError):
            await left.request("slow", timeout=0.03)
        await asyncio.wait_for(cancelled.wait(), 1)


async def test_large_response_becomes_bounded_error_and_peer_remains_usable():
    async def handler(method, params):
        return "x" * (1024 * 1024) if method == "large" else "ok"

    async with peers(handler, handler) as (left, _):
        with pytest.raises(PluginError) as error:
            await left.request("large")
        assert error.value.code == "frame_too_large"
        assert await left.request("small") == "ok"


async def test_disconnect_settles_pending_requests():
    async def handler(method, params):
        await asyncio.Event().wait()

    async with peers(handler, handler) as (left, right):
        pending = asyncio.create_task(left.request("slow"))
        await asyncio.sleep(0.01)
        await right.close()
        with pytest.raises(PluginError) as error:
            await pending
        assert error.value.code == "worker_disconnected"
