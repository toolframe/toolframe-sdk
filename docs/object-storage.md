# Object storage

Toolframe's `object-store` providers keep binary objects separate from SQL data.
Business plugins use the public SDK and declare the same service for local disk
and S3-compatible storage:

```python
from toolframe_sdk import Requirement

# In PluginDefinition:
requires={"objects": Requirement(service="toolframe.object_store", version=1)}

# In an admitted service handler or context.task():
objects = self.context.object_store()
metadata = await objects.put("images/avatar.png", png_bytes, content_type="image/png")
image = await objects.get(metadata["key"])
page = await objects.list(prefix="images/", limit=20)
next_page = await objects.list(prefix="images/", after=page["next_after"], limit=20) if page["next_after"] else None
await objects.delete(metadata["key"])
```

This API is part of the object-storage development revision of SDK 0.3.0. Build
matching SDK/Core wheels from this revision until it receives a published release.
Core must recognize the `object-store` provider kind and allocate its state path.

## Ownership and authorization

`toolframe.object_store` is internal. Core supplies application ID, calling plugin,
generation, deadline and principal; callers cannot select an owner or bucket.
Object keys are relative ASCII names, at most 512 characters, using letters,
digits, `/`, `.`, `_` and `-`; empty, dot and parent path segments are rejected.
Cross-plugin access goes through the owning business service.

Define business read/write scopes on your own service. Dashboard pages call that
service through `PageContext.services`, and display returned bytes with
`st.image`. Use an inline data URL for scoped images so Streamlit does not create
a shared media URL outside the owning business method's admission. Keep file keys in SQL rows when they belong to another business record.
SQL and object storage do not share a transaction: use application compensation or
an orphan-reconciliation job when updating both. Deleting a row does not delete
its objects automatically.

No public bucket, filesystem path, presigned URL or arbitrary file-serving route
is returned. Business plugins decide which objects a visitor may access. Plugins
are trusted Python code; the scoping contract is not an OS sandbox. AWS environment
credentials inherited by workers must be treated as application-level credentials.

## Transfers and limits

`put` and `get` accept/return bytes in plugin memory. On the wire, the SDK sends
256 KiB base64 chunks, keeping every message below the 1 MiB JSON-RPC limit.
Objects are limited to 64 MiB; a provider's `max_object_bytes` may lower this.
Every upload declares its size, MIME type (without parameters), and SHA-256.
Content checksums are returned as `etag`; these are not S3's native ETags.

For a streaming producer, use `begin_upload(key, size, content_type=..., sha256=...)`,
`upload_chunk(upload_id, offset, bytes)`, then `complete_upload(upload_id)`.
`abort_upload(upload_id)` discards an unfinished transfer. Chunks are ordered;
replaying identical bytes at an already written offset is safe. Completion validates
the size and digest before publishing. Metadata contains `key`, `size`,
`content_type`, `etag`, and `modified_at` (Unix seconds).

Upload IDs are unguessable and bound to the application, owning plugin, owner
generation and admitted actor. They can span separate service calls/roots, which
lets Dashboard upload forms transfer large files. Each call still needs admission.
Uploads expire ten minutes after creation; there are at most 32 unfinished uploads
and 256 MiB of reserved staging space per provider. Staging uses temporary files;
expiry, abort, disposal and process exit release them. Reload invalidates unfinished
uploads. Completed objects survive reload, disable and removal.

Completion acknowledgements can be retried while their in-memory receipt remains
(up to ten minutes and the most recent 128 receipts). They are not durable mutation
receipts. On an unknown result after a disconnect/restart, inspect `head(key)` and
compare its checksum. Publication is atomic; replacing a key is last-writer-wins.
Concurrent readers use `head` plus `read_chunk(..., etag=...)`; a replacement with
different content fails with `object_changed` instead of mixing versions.

Listings return at most 100 objects and a `next_after` key cursor. They are not
snapshot transactions. Concurrent writes/deletes may change later pages. Delete
is idempotent. Missing objects report `object_missing`.

## Provider authors

`toolframe_sdk.object_store.object_store_service()` declares the v1 wire contract.
`toolframe_sdk.object_store_provider.ObjectStoreProvider` implements identity,
validation, staging, checksums and cleanup. Subclasses provide synchronous
`open_backend`, `put`, `head`, `read`, `list`, `delete`, and optionally
`close_backend` methods. Namespace arguments are host-derived, never user paths.
Backend work runs on a thread, serialized per provider; cancellation waits for an
in-flight backend operation before closing its resources.

Exactly one object-store provider is enabled per application, independently of the
SQL provider. Selecting another backend does not migrate existing objects. Keep
the application ID stable and migrate/copy data explicitly before switching.
S3 providers use `runtime_validation="separate_application"` so Core does not
probe shared remote storage as if it were disposable local state.
