"""Run the frozen SDK 0.4 consumer through the current real worker."""

import asyncio
import os
import sys
from pathlib import Path

from toolframe_sdk import PROTOCOL_VERSION, PluginDefinition
from toolframe_sdk.rpc import RpcPeer


async def test_sdk04_consumer_keeps_its_wire_contract_and_behavior():
    fixture = Path(__file__).parent / "fixtures"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "toolframe_sdk.worker",
        "--entrypoint",
        "sdk04_plugin:plugin",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env={**os.environ, "PYTHONPATH": str(fixture)},
    )

    async def unavailable(method, params):
        raise AssertionError(f"Unexpected host call: {method}")

    peer = RpcPeer(process.stdout, process.stdin, unavailable)
    reader = asyncio.create_task(peer.run())
    try:
        report = await peer.request("runtime.describe")
        assert report["protocol"] == PROTOCOL_VERSION == 1
        definition = PluginDefinition.model_validate(report["definition"])
        method = definition.services["compat-echo.messages"].methods["echo"]
        assert method.required_scopes == ("compat-echo.read",)
        assert method.public_api and method.read_only
        await peer.request("runtime.activate", {"generation": "baseline", "config": {}})
        result = await peer.request(
            "runtime.invoke",
            {
                "service": "compat-echo.messages",
                "method": "echo",
                "params": {"message": "unchanged plugin"},
                "context": {"root_id": "baseline"},
            },
        )
        assert result == {"message": "unchanged plugin"}
    finally:
        await peer.close()
        await asyncio.gather(reader, return_exceptions=True)
        try:
            await asyncio.wait_for(process.wait(), 10)
        except TimeoutError:
            process.kill()
            await process.wait()
