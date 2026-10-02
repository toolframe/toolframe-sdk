import asyncio
import json
import os
import sys

from toolframe_sdk.rpc import RpcPeer


async def test_real_worker_describes_validates_and_cleans_up_on_eof(tmp_path):
    (tmp_path / "fixture_plugin.py").write_text("""
from toolframe_sdk import AuthorizationSpec, ScopeSpec, Plugin, PluginDefinition, ServiceSpec, MethodSpec
print("library stdout is a log")
class Example(Plugin):
    async def invoke(self, service, method, params):
        self.log.info("Calling dependency", extra={"number": params["number"]})
        return {"value": await self.context.call("dependency", "double", params)}
    async def dispose(self):
        from pathlib import Path
        Path(self.context.config["marker"]).write_text("disposed")
        await super().dispose()
plugin = PluginDefinition(authorization=AuthorizationSpec(), id="example", factory="fixture_plugin:Example",
    config_schema={"type":"object"},
    services={"example":ServiceSpec(methods={"run":MethodSpec(audience="internal", )})})
""")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "toolframe_sdk.worker",
        "--entrypoint",
        "fixture_plugin:plugin",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
    )

    async def handler(method, params):
        assert method == "service.call"
        return params["params"]["number"] * 2

    peer = RpcPeer(process.stdout, process.stdin, handler)
    reader = asyncio.create_task(peer.run())
    try:
        report = await peer.request("runtime.describe")
        assert report["definition"]["id"] == "example"
        await peer.request(
            "runtime.activate",
            {"generation": "g1", "config": {"marker": str(tmp_path / "closed")}},
        )
        assert await peer.request(
            "runtime.invoke",
            {
                "service": "example",
                "method": "run",
                "params": {"number": 4},
                "context": {"root_id": "r1"},
            },
        ) == {"value": 8}
        process.stdin.close()
        await asyncio.wait_for(process.wait(), 5)
        assert (tmp_path / "closed").read_text() == "disposed"
        stderr = (await process.stderr.read()).decode().splitlines()
        assert "library stdout is a log" in stderr
        logs = [json.loads(line) for line in stderr if line.startswith("{")]
        call = next(log for log in logs if log["message"] == "Calling dependency")
        assert call["attributes"]["toolframe.plugin.id"] == "example"
        assert call["attributes"]["toolframe.plugin.generation"] == "g1"
        assert call["attributes"]["toolframe.service.name"] == "example"
        assert call["attributes"]["toolframe.method.name"] == "run"
        assert call["attributes"]["toolframe.root.id"] == "r1"
        assert call["attributes"]["code.function.name"] == "invoke"
        assert process.returncode == 0
    finally:
        await peer.close()
        await asyncio.gather(reader, return_exceptions=True)
        if process.returncode is None:
            process.kill()
            await process.wait()
