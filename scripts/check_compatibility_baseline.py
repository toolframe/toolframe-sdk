"""Invoke the unchanged SDK 0.4 fixture wheel using an installed SDK."""

import asyncio
import hashlib
import json
import sys
from importlib import metadata
from pathlib import Path

import sdk04_plugin

from toolframe_sdk import PROTOCOL_VERSION, PluginDefinition
from toolframe_sdk.rpc import RpcPeer


async def main():
    wheel = Path(sys.argv[1])
    distribution = metadata.distribution("toolframe-sdk04-compat-fixture")
    assert distribution.version == "0.1.0"
    assert distribution.requires == ["toolframe-sdk<1,>=0.4.0"]
    assert (
        Path(sdk04_plugin.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-m",
        "toolframe_sdk.worker",
        "--entrypoint",
        "sdk04_plugin:plugin",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )

    async def unavailable(method, params):
        raise AssertionError(f"Unexpected host call: {method}")

    peer = RpcPeer(process.stdout, process.stdin, unavailable)
    reader = asyncio.create_task(peer.run())
    try:
        async with asyncio.timeout(30):
            report = await peer.request("runtime.describe")
            assert report["protocol"] == PROTOCOL_VERSION == 1
            definition = PluginDefinition.model_validate(report["definition"])
            method = definition.services["compat-echo.messages"].methods["echo"]
            assert method.required_scopes == ("compat-echo.read",)
            assert method.public_api and method.read_only
            await peer.request(
                "runtime.activate", {"generation": "retained-wheel", "config": {}}
            )
            result = await peer.request(
                "runtime.invoke",
                {
                    "service": "compat-echo.messages",
                    "method": "echo",
                    "params": {"message": "unchanged plugin wheel"},
                    "context": {"root_id": "retained-wheel"},
                },
            )
            assert result == {"message": "unchanged plugin wheel"}
    finally:
        await peer.close()
        await asyncio.gather(reader, return_exceptions=True)
        try:
            await asyncio.wait_for(process.wait(), 10)
        except TimeoutError:
            process.kill()
            await process.wait()
    print(
        json.dumps(
            {
                "sdk": metadata.version("toolframe-sdk"),
                "plugin_wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                "protocol": PROTOCOL_VERSION,
                "passed": True,
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
