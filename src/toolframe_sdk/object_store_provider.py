"""Reusable transfer protocol for providers; persistence is supplied by a backend.

Backends implement synchronous put/head/read/list/delete over a trusted namespace.
Calls are serialized off the event loop, including completion after cancellation.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import re
import secrets
import tempfile
import time
from dataclasses import dataclass

from jsonschema.exceptions import ValidationError

from .contracts import PluginError, digest, validate
from .object_store import (
    CHUNK_BYTES,
    MAX_OBJECT_BYTES,
    OBJECT_STORE_SERVICE,
    object_store_service,
)
from .runtime import Plugin


def check_key(key, *, prefix=False):
    if prefix and key == "":
        return key
    if (
        not isinstance(key, str)
        or len(key) > 512
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", key)
    ):
        raise PluginError(
            "invalid_key", "Use a relative ASCII object key of at most 512 characters"
        )
    parts = key.split("/")
    if prefix and parts[-1] == "":
        parts.pop()
    if any(part in {"", ".", ".."} for part in parts):
        raise PluginError(
            "invalid_key", "Object keys cannot contain empty, dot or parent segments"
        )
    return key


@dataclass
class Upload:
    identity: tuple
    metadata: dict
    file: object
    expires: float
    offset: int = 0
    result: dict | None = None


class ObjectStoreProvider(Plugin):
    """Bounded, expiring upload sessions with application/owner/actor isolation."""

    upload_ttl = 600
    max_uploads = 32
    max_staged_bytes = 256 * 1024 * 1024

    def __init__(self, context):
        super().__init__(context)
        self.uploads: dict[str, Upload] = {}
        self.lock = asyncio.Lock()
        self.sweeper = None
        self.spec = object_store_service()

    async def _io(self, function, *args):
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        cancellation = None
        while True:
            try:
                result = await asyncio.shield(task)
            except asyncio.CancelledError as error:
                if task.cancelled():
                    raise
                # A thread cannot be cancelled. Repeated cancellation must also
                # retain the provider lock and upload file until it settles.
                cancellation = error
            except Exception:
                if cancellation is not None:
                    raise cancellation from None
                raise
            else:
                if cancellation is not None:
                    raise cancellation
                return result

    async def activate(self):
        async with self.lock:
            await self._io(self.open_backend)
        self.sweeper = asyncio.create_task(self._sweep())

    def open_backend(self):
        raise NotImplementedError

    def close_backend(self):
        pass

    async def _sweep(self):
        while True:
            await asyncio.sleep(30)
            async with self.lock:
                self._expire()

    def _expire(self):
        for token, upload in list(self.uploads.items()):
            if upload.expires <= time.time():
                upload.file.close()
                del self.uploads[token]

    async def dispose(self):
        if self.sweeper:
            self.sweeper.cancel()
            await asyncio.gather(self.sweeper, return_exceptions=True)
        async with self.lock:
            for upload in self.uploads.values():
                upload.file.close()
            self.uploads.clear()
            await self._io(self.close_backend)
        await super().dispose()

    def identity(self):
        info = self.context.call_info
        owner = info.get("owner", "")
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", owner):
            raise PluginError(
                "identity_missing", "A host-issued plugin owner is required"
            )
        if not all(
            info.get(key) for key in ("application_id", "owner_generation", "root_id")
        ):
            raise PluginError(
                "identity_missing",
                "A host-issued application, generation and root are required",
            )
        if info.get("deadline", 0) <= time.time():
            raise PluginError("deadline_exceeded", "Object storage call has expired")
        principal = info.get("principal") or {}
        actor = digest(
            {
                key: principal.get(key)
                for key in (
                    "kind",
                    "credential_id",
                    "user_id",
                    "client_id",
                    "grant_id",
                    "resource",
                )
            }
        )
        application = hashlib.sha256(info["application_id"].encode()).hexdigest()
        return (f"{application}/{owner}", info["owner_generation"], actor)

    async def invoke(self, service, method, params):
        if service != OBJECT_STORE_SERVICE or method not in self.spec.methods:
            raise PluginError("method_not_found", "Unknown object storage method")
        try:
            validate(self.spec.methods[method].input_schema, params)
        except ValidationError, TypeError, ValueError:
            raise PluginError(
                "invalid_params", "Invalid object storage parameters"
            ) from None
        async with self.lock:
            identity = (
                self.identity()
            )  # Recheck deadline after waiting for another request.
            self._expire()
            try:
                return await self._io(self._invoke, identity, method, params)
            except PluginError:
                raise
            except Exception:  # noqa: BLE001 -- sanitize backend failures at the protocol boundary.
                # Backend exceptions can contain endpoint credentials, paths or bucket names.
                raise PluginError(
                    "object_store_unavailable", "Object storage operation failed"
                ) from None

    def _invoke(self, identity, method, params):
        namespace = identity[0]
        if "key" in params:
            check_key(params["key"])
        if method == "begin_upload":
            if not re.fullmatch(
                r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", params["content_type"]
            ):
                raise PluginError(
                    "invalid_content_type", "Use a MIME type without parameters"
                )
            maximum = self.context.config.get("max_object_bytes", MAX_OBJECT_BYTES)
            if params["size"] > maximum:
                raise PluginError(
                    "object_too_large", "Object exceeds the configured size limit"
                )
            pending = [u for u in self.uploads.values() if u.result is None]
            if (
                len(pending) >= self.max_uploads
                or sum(u.metadata["size"] for u in pending) + params["size"]
                > self.max_staged_bytes
            ):
                raise PluginError(
                    "upload_capacity",
                    "Upload staging is full; finish or abort an upload",
                )
            # Retain a bounded completion receipt for safe acknowledgement retries.
            completed = [
                key for key, value in self.uploads.items() if value.result is not None
            ]
            for token in completed[:-127]:
                del self.uploads[token]
            token = secrets.token_hex(32)
            self.uploads[token] = Upload(
                identity,
                {
                    "key": params["key"],
                    "size": params["size"],
                    "content_type": params["content_type"],
                    "etag": params["sha256"],
                },
                tempfile.TemporaryFile(),  # noqa: SIM115 -- lifetime spans calls; expiry/dispose close it.
                time.time() + self.upload_ttl,
            )
            return {
                "upload_id": token,
                "chunk_bytes": CHUNK_BYTES,
                "expires_at": self.uploads[token].expires,
            }
        if method in {"upload_chunk", "complete_upload", "abort_upload"}:
            upload = self.uploads.get(params["upload_id"])
            if upload is None or upload.identity != identity:
                raise PluginError(
                    "upload_missing",
                    "Upload expired or belongs to another caller or generation",
                )
            if method == "abort_upload":
                upload.file.close()
                del self.uploads[params["upload_id"]]
                return {"aborted": True}
            if upload.result is not None:
                if method == "complete_upload":
                    return upload.result
                raise PluginError("upload_completed", "Upload has already completed")
            if method == "upload_chunk":
                try:
                    data = base64.b64decode(params["data"], validate=True)
                except ValueError, binascii.Error:
                    raise PluginError(
                        "invalid_chunk", "Chunk must be valid base64"
                    ) from None
                offset = params["offset"]
                if (
                    not data
                    or len(data) > CHUNK_BYTES
                    or offset + len(data) > upload.metadata["size"]
                ):
                    raise PluginError(
                        "invalid_chunk", "Chunk is empty or exceeds the declared size"
                    )
                if offset < upload.offset:
                    upload.file.seek(offset)
                    if (
                        offset + len(data) > upload.offset
                        or upload.file.read(len(data)) != data
                    ):
                        raise PluginError(
                            "upload_offset",
                            "Retried chunk differs from the stored bytes",
                        )
                elif offset == upload.offset:
                    upload.file.seek(offset)
                    upload.file.write(data)
                    upload.offset += len(data)
                else:
                    raise PluginError(
                        "upload_offset", "Chunks must be uploaded in order"
                    )
                return {"offset": upload.offset}
            if upload.offset != upload.metadata["size"]:
                raise PluginError(
                    "upload_incomplete", "Upload has not reached its declared size"
                )
            upload.file.seek(0)
            if (
                hashlib.file_digest(upload.file, "sha256").hexdigest()
                != upload.metadata["etag"]
            ):
                raise PluginError(
                    "checksum_mismatch", "Upload does not match its declared SHA-256"
                )
            upload.file.seek(0)
            metadata = {**upload.metadata, "modified_at": time.time()}
            self.put(namespace, metadata, upload.file)
            upload.result = metadata
            upload.file.close()
            return metadata
        if method == "head":
            return self.head(namespace, params["key"])
        if method == "read_chunk":
            data = self.read(
                namespace,
                params["key"],
                params["offset"],
                params["length"],
                params["etag"],
            )
            return {"data": base64.b64encode(data).decode("ascii")}
        if method == "list":
            check_key(params["prefix"], prefix=True)
            if params["after"]:
                check_key(params["after"])
            return self.list(
                namespace, params["prefix"], params["after"], params["limit"]
            )
        self.delete(namespace, params["key"])
        return {"deleted": True}
