import asyncio
import hashlib
import time
from types import SimpleNamespace

import pytest

from toolframe_sdk import PluginError, RuntimeContext, call_context
from toolframe_sdk.object_store import CHUNK_BYTES, OBJECT_STORE_SERVICE
from toolframe_sdk.object_store_provider import ObjectStoreProvider, check_key


class MemoryStore(ObjectStoreProvider):
    def open_backend(self):
        self.objects = {}

    def put(self, namespace, metadata, stream):
        self.objects[namespace, metadata["key"]] = (dict(metadata), stream.read())

    def head(self, namespace, key):
        try:
            return self.objects[namespace, key][0]
        except KeyError:
            raise PluginError("object_missing", "Missing") from None

    def read(self, namespace, key, offset, length, etag):
        metadata = self.head(namespace, key)
        if metadata["etag"] != etag:
            raise PluginError("object_changed", "Changed")
        return self.objects[namespace, key][1][offset : offset + length]

    def list(self, namespace, prefix, after, limit):
        items = sorted(
            (
                m
                for (n, k), (m, _) in self.objects.items()
                if n == namespace and k.startswith(prefix) and k > after
            ),
            key=lambda m: m["key"],
        )
        return {
            "objects": items[:limit],
            "next_after": items[limit - 1]["key"] if len(items) > limit else None,
        }

    def delete(self, namespace, key):
        self.objects.pop((namespace, key), None)


def identity(**extra):
    return {
        "application_id": "app",
        "owner": "gallery",
        "owner_generation": "g1",
        "root_id": "root",
        "deadline": time.time() + 60,
        "principal": {"kind": "user", "user_id": "alice"},
        **extra,
    }


@pytest.fixture
async def store():
    provider = MemoryStore(RuntimeContext(None, plugin_id="store", generation="s1"))
    await provider.activate()

    async def request(method, params, **kwargs):
        assert method == "service.call"
        return await provider.invoke(
            OBJECT_STORE_SERVICE, params["method"], params["params"]
        )

    context = RuntimeContext(
        SimpleNamespace(request=request), plugin_id="gallery", generation="g1"
    )
    token = call_context.set(identity())
    try:
        yield provider, context.object_store()
    finally:
        call_context.reset(token)
        await provider.dispose()


async def test_large_roundtrip_replace_list_delete_and_empty(store):
    _provider, client = store
    data = b"large binary\x00" * 130000
    metadata = await client.put("images/photo.png", data, content_type="image/png")
    assert metadata["etag"] == hashlib.sha256(data).hexdigest()
    assert await client.get("images/photo.png") == data
    await client.put("empty", b"")
    assert await client.get("empty") == b""
    page = await client.list(limit=1)
    assert page["next_after"] == "empty"
    assert (await client.list(after=page["next_after"]))["objects"][0][
        "key"
    ] == "images/photo.png"
    await client.put("images/photo.png", b"replacement")
    with pytest.raises(PluginError, match="Changed"):
        await client.read_chunk("images/photo.png", etag=metadata["etag"])
    await client.delete("images/photo.png")
    await client.delete("images/photo.png")
    with pytest.raises(PluginError) as error:
        await client.get("images/photo.png")
    assert error.value.code == "object_missing"


@pytest.mark.parametrize(
    "key",
    [
        "",
        "../secret",
        "/absolute",
        "a//b",
        "a/../b",
        "a/./b",
        "a\\b",
        "x\x00",
        "x" * 513,
    ],
)
def test_bad_keys(key):
    with pytest.raises(PluginError):
        check_key(key)


async def test_upload_binding_and_cross_root_progress(store):
    _provider, client = store
    upload = await client.begin_upload(
        "photo", 3, content_type="image/png", sha256=hashlib.sha256(b"abc").hexdigest()
    )
    for override in (
        {"application_id": "other"},
        {"owner": "other"},
        {"owner_generation": "g2"},
        {"principal": {"kind": "user", "user_id": "bob"}},
    ):
        token = call_context.set(identity(**override))
        try:
            with pytest.raises(PluginError) as error:
                await client.upload_chunk(upload["upload_id"], 0, b"abc")
            assert error.value.code == "upload_missing"
        finally:
            call_context.reset(token)
    token = call_context.set(identity(root_id="different-root"))
    try:
        await client.upload_chunk(upload["upload_id"], 0, b"abc")
        await client.upload_chunk(upload["upload_id"], 0, b"abc")
        result = await client.complete_upload(upload["upload_id"])
        assert await client.complete_upload(upload["upload_id"]) == result
    finally:
        call_context.reset(token)
    token = call_context.set(identity(owner="other"))
    try:
        assert not (await client.list())["objects"]
    finally:
        call_context.reset(token)


async def test_incomplete_corrupt_invalid_chunks_expiry_and_capacity(store):
    provider, client = store
    token = (
        await client.begin_upload("x", 3, content_type="image/png", sha256="0" * 64)
    )["upload_id"]
    with pytest.raises(PluginError) as error:
        await client.complete_upload(token)
    assert error.value.code == "upload_incomplete"
    for offset, data in [(1, b"a"), (0, b"abcd"), (0, b"")]:
        with pytest.raises(PluginError):
            await client.upload_chunk(token, offset, data)
    with pytest.raises(PluginError):
        await provider.invoke(
            OBJECT_STORE_SERVICE,
            "upload_chunk",
            {"upload_id": token, "offset": 0, "data": "***"},
        )
    await client.upload_chunk(token, 0, b"abc")
    with pytest.raises(PluginError) as error:
        await client.complete_upload(token)
    assert error.value.code == "checksum_mismatch"
    provider.max_uploads = 1
    with pytest.raises(PluginError) as error:
        await client.put("second", b"a")
    assert error.value.code == "upload_capacity"
    provider.uploads[token].expires = 0
    assert await client.put("second", b"a")
    assert token not in provider.uploads


async def test_identity_deadline_schema_and_size(store):
    provider, client = store
    for info, code in [
        ({}, "identity_missing"),
        (identity(deadline=0), "deadline_exceeded"),
    ]:
        token = call_context.set(info)
        try:
            with pytest.raises(PluginError) as error:
                await provider.invoke(OBJECT_STORE_SERVICE, "head", {"key": "x"})
            assert error.value.code == code
        finally:
            call_context.reset(token)
    with pytest.raises(PluginError) as error:
        await provider.invoke(
            OBJECT_STORE_SERVICE, "head", {"key": "x", "owner": "victim"}
        )
    assert error.value.code == "invalid_params"
    provider.context.config["max_object_bytes"] = 1
    with pytest.raises(PluginError) as error:
        await client.put("x", b"ab")
    assert error.value.code == "object_too_large"
    with pytest.raises(PluginError):
        await client.upload_chunk("0" * 64, 0, b"a" * (CHUNK_BYTES + 1))


async def test_dispose_closes_abandoned_uploads(store):
    provider, client = store
    token = (
        await client.begin_upload("x", 1, content_type="image/png", sha256="0" * 64)
    )["upload_id"]
    file = provider.uploads[token].file
    await provider.dispose()
    assert file.closed and not provider.uploads


@pytest.mark.parametrize("cancellations", [1, 3])
@pytest.mark.parametrize("backend_fails", [False, True])
async def test_cancelled_backend_finishes_before_disposal(
    store, cancellations, backend_fails
):
    provider, client = store
    import threading

    started, finish = threading.Event(), threading.Event()
    original = provider.put

    def slow(*args):
        started.set()
        assert finish.wait(timeout=3)
        if backend_fails:
            raise OSError("backend failed after cancellation")
        return original(*args)

    provider.put = slow
    upload = await client.begin_upload(
        "x", 1, content_type="image/png", sha256=hashlib.sha256(b"a").hexdigest()
    )
    file = provider.uploads[upload["upload_id"]].file
    await client.upload_chunk(upload["upload_id"], 0, b"a")
    task = asyncio.create_task(client.complete_upload(upload["upload_id"]))
    disposal = None
    try:
        assert await asyncio.to_thread(started.wait, 3)
        for _ in range(cancellations):
            task.cancel()
            await asyncio.sleep(0.01)
        disposal = asyncio.create_task(provider.dispose())
        await asyncio.sleep(0.01)
        assert not task.done()
        assert not disposal.done()
        assert provider.lock.locked()
        assert not file.closed
    finally:
        finish.set()
        outcomes = await asyncio.gather(
            task, *([disposal] if disposal else []), return_exceptions=True
        )
    assert isinstance(outcomes[0], asyncio.CancelledError)
    assert outcomes[1] is None
    assert file.closed
    if not backend_fails:
        assert next(iter(provider.objects.values()))[1] == b"a"
