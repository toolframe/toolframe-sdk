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
from .logging import ContextLogger
from .object_store import ObjectStoreClient
from .page import PageContext
from .runtime import Plugin, RuntimeContext, SqlClient, call_context
from .web import WebApp, WebContext, WebRequest, WebResponse, WebRoute, WebUpload

__version__ = "0.4.0+web.1"
__all__ = [
    "MAX_FRAME_BYTES",
    "PROTOCOL_VERSION",
    "SYSTEM_SCOPES",
    "AuthorizationSpec",
    "ContextLogger",
    "MethodSpec",
    "Migration",
    "ObjectStoreClient",
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
    "SqlClient",
    "StoreSchema",
    "WebApp",
    "WebContext",
    "WebRequest",
    "WebResponse",
    "WebRoute",
    "WebUpload",
    "call_context",
    "digest",
    "load_object",
    "validate",
]
