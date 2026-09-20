import asyncio

import pytest

from toolframe_sdk import OwnedResource, PageContext, PluginError, Resource


async def test_native_page_bridge_uses_owning_resource_and_epoch():
    received = []

    class Runtime:
        async def resource_call(self, *args, **kwargs):
            received.append((args, kwargs))
            return {"rows": [1]}

    resource = OwnedResource(
        owner="example",
        generation="g1",
        resource=Resource(
            id="main", kind="dashboard.page", data={"entrypoint": "example:render"}
        ),
    )
    context = PageContext(Runtime(), resource, "epoch-1", asyncio.get_running_loop())
    service = context.services.require("example.rows")
    result = await asyncio.to_thread(service.call, "list", {"limit": 1})
    assert result == {"rows": [1]}
    assert received == [
        (
            ("example/dashboard.page/main", "example.rows@1/list", {"limit": 1}),
            {
                "epoch": "epoch-1",
                "timeout": 30,
                "access_context": None,
                "activity": True,
            },
        )
    ]
    with pytest.raises(PluginError, match="script thread"):
        service.call("list")
