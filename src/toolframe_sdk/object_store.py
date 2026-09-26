"""Scoped object storage client and portable, bounded JSON transfer contract."""

from __future__ import annotations

import base64
import contextlib
import hashlib

from .contracts import MethodSpec, PluginError, ServiceSpec

OBJECT_STORE_SERVICE = "toolframe.object_store"
CHUNK_BYTES = 256 * 1024
MAX_OBJECT_BYTES = 64 * 1024 * 1024
KEY_SCHEMA = {"type": "string", "minLength": 1, "maxLength": 512}
TOKEN_SCHEMA = {"type": "string", "pattern": "^[a-f0-9]{64}$"}


def object_store_service() -> ServiceSpec:
    """A fresh service declaration for either backend and scoped business adapters."""
    fields = {
        "begin_upload": {
            "key": KEY_SCHEMA,
            "size": {"type": "integer", "minimum": 0, "maximum": MAX_OBJECT_BYTES},
            "content_type": {"type": "string", "maxLength": 128},
            "sha256": TOKEN_SCHEMA,
        },
        "upload_chunk": {
            "upload_id": TOKEN_SCHEMA,
            "offset": {"type": "integer", "minimum": 0},
            "data": {"type": "string", "maxLength": 4 * ((CHUNK_BYTES + 2) // 3)},
        },
        "complete_upload": {"upload_id": TOKEN_SCHEMA},
        "abort_upload": {"upload_id": TOKEN_SCHEMA},
        "head": {"key": KEY_SCHEMA},
        "read_chunk": {
            "key": KEY_SCHEMA,
            "offset": {"type": "integer", "minimum": 0},
            "length": {"type": "integer", "minimum": 1, "maximum": CHUNK_BYTES},
            "etag": TOKEN_SCHEMA,
        },
        "list": {
            "prefix": {"type": "string", "maxLength": 512},
            "after": {"type": "string", "maxLength": 512},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "delete": {"key": KEY_SCHEMA},
    }
    return ServiceSpec(
        methods={
            name: MethodSpec(
                audience="internal",
                read_only=name in {"head", "read_chunk", "list"},
                input_schema={
                    "type": "object",
                    "properties": properties,
                    "required": list(properties),
                    "additionalProperties": False,
                },
            )
            for name, properties in fields.items()
        }
    )


class ObjectStoreClient:
    """Bytes convenience API; explicit transfer methods allow streamed producers."""

    def __init__(self, context, alias="objects"):
        self.context, self.alias = context, alias

    async def begin_upload(self, key, size, *, content_type, sha256):
        return await self.context.call(
            self.alias,
            "begin_upload",
            {
                "key": key,
                "size": size,
                "content_type": content_type,
                "sha256": sha256,
            },
        )

    async def upload_chunk(self, upload_id, offset, data):
        if len(data) > CHUNK_BYTES:
            raise PluginError("chunk_too_large", "Use chunks of at most 256 KiB")
        return await self.context.call(
            self.alias,
            "upload_chunk",
            {
                "upload_id": upload_id,
                "offset": offset,
                "data": base64.b64encode(data).decode("ascii"),
            },
        )

    async def complete_upload(self, upload_id):
        return await self.context.call(
            self.alias, "complete_upload", {"upload_id": upload_id}
        )

    async def abort_upload(self, upload_id):
        return await self.context.call(
            self.alias, "abort_upload", {"upload_id": upload_id}
        )

    async def put(self, key, data: bytes, *, content_type="application/octet-stream"):
        if len(data) > MAX_OBJECT_BYTES:
            raise PluginError("object_too_large", "Objects are limited to 64 MiB")
        upload = await self.begin_upload(
            key,
            len(data),
            content_type=content_type,
            sha256=hashlib.sha256(data).hexdigest(),
        )
        try:
            for offset in range(0, len(data), CHUNK_BYTES):
                await self.upload_chunk(
                    upload["upload_id"], offset, data[offset : offset + CHUNK_BYTES]
                )
            return await self.complete_upload(upload["upload_id"])
        except BaseException:
            # Expiry/disposal also reclaims an upload when its caller has disconnected.
            with contextlib.suppress(PluginError, TimeoutError):
                await self.abort_upload(upload["upload_id"])
            raise

    async def head(self, key):
        return await self.context.call(self.alias, "head", {"key": key})

    async def read_chunk(self, key, *, offset=0, length=CHUNK_BYTES, etag):
        result = await self.context.call(
            self.alias,
            "read_chunk",
            {
                "key": key,
                "offset": offset,
                "length": length,
                "etag": etag,
            },
        )
        return base64.b64decode(result["data"], validate=True)

    async def get(self, key) -> bytes:
        metadata = await self.head(key)
        if metadata["size"] > MAX_OBJECT_BYTES:
            raise PluginError("object_too_large", "Objects are limited to 64 MiB")
        result = bytearray()
        while len(result) < metadata["size"]:
            part = await self.read_chunk(key, offset=len(result), etag=metadata["etag"])
            if not part:
                raise PluginError(
                    "object_changed", "Object ended before its declared size"
                )
            result.extend(part)
        if (
            len(result) != metadata["size"]
            or hashlib.sha256(result).hexdigest() != metadata["etag"]
        ):
            raise PluginError("object_changed", "Object changed or failed its checksum")
        return bytes(result)

    async def list(self, *, prefix="", after="", limit=100):
        return await self.context.call(
            self.alias,
            "list",
            {
                "prefix": prefix,
                "after": after,
                "limit": limit,
            },
        )

    async def delete(self, key):
        return await self.context.call(self.alias, "delete", {"key": key})
