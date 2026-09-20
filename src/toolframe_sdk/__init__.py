"""Toolframe public plugin authoring interface; no provider implementation imports."""

from .contracts import (
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    SYSTEM_SCOPES,
    AuthorizationSpec,
    MethodSpec,
    Migration,
    OwnedResource,
    PluginDefinition,
    PluginError,
    Principal,
    Requirement,
    Resource,
    ScopeSpec,
    ServiceSpec,
    StoreSchema,
    digest,
    load_object,
    validate,
)
from .page import PageContext
from .runtime import Plugin, RuntimeContext, StorageClient, call_context

__version__ = "0.2.0"
__all__ = [
    "MAX_FRAME_BYTES",
    "PROTOCOL_VERSION",
    "SYSTEM_SCOPES",
    "AuthorizationSpec",
    "MethodSpec",
    "Migration",
    "OwnedResource",
    "PageContext",
    "Plugin",
    "PluginDefinition",
    "PluginError",
    "Principal",
    "Requirement",
    "Resource",
    "RuntimeContext",
    "ScopeSpec",
    "ServiceSpec",
    "StorageClient",
    "StoreSchema",
    "call_context",
    "digest",
    "load_object",
    "validate",
]
