"""Serializable definitions shared by independently installed plugins and hosts."""

from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import PurePosixPath
from typing import Any, Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, model_validator

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 1024 * 1024
Identifier = str


def digest(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(data.encode()).hexdigest()


def load_object(reference: str) -> Any:
    module, separator, attribute = reference.partition(":")
    if not separator or not module or not attribute:
        raise ValueError("Entry references must be module:attribute")
    result = importlib.import_module(module)
    for part in attribute.split("."):
        if part.startswith("_"):
            raise ValueError("Private entry references are not supported")
        result = getattr(result, part)
    return result


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MethodSpec(Contract):
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    output_schema: dict[str, Any] = Field(default_factory=dict)
    description: str = ""
    read_only: bool = False

    @model_validator(mode="after")
    def check_schemas(self):
        Draft202012Validator.check_schema(self.input_schema)
        Draft202012Validator.check_schema(self.output_schema)
        return self


class ServiceSpec(Contract):
    version: int = Field(default=1, ge=1)
    methods: dict[str, MethodSpec]


class Requirement(Contract):
    service: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    version: int = Field(default=1, ge=1)
    optional: bool = False


class Resource(Contract):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    kind: Literal["dashboard.page", "mcp.tool", "llms.document"]
    title: str = ""
    data: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_resource(self):
        if self.kind == "dashboard.page":
            if ":" not in self.data.get("entrypoint", ""):
                raise ValueError("Page requires a module:render entrypoint")
        elif self.kind == "mcp.tool":
            if not self.data.get("service") or not self.data.get("method"):
                raise ValueError("Tool requires a service and method")
            for field in ("input_schema", "output_schema"):
                Draft202012Validator.check_schema(self.data.get(field, {}))
        else:
            path = PurePosixPath(self.data.get("path", ""))
            if (
                not str(path)
                or str(path) == "."
                or path.is_absolute()
                or ".." in path.parts
            ):
                raise ValueError("Document path must stay inside its package")
            if not self.data.get("package"):
                raise ValueError("Document requires its import package")
        return self


class Migration(Contract):
    revision: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    parent: str | None = None
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()


class StoreSchema(Contract):
    revision: str
    migrations: tuple[Migration, ...]

    @model_validator(mode="after")
    def check_history(self):
        previous = None
        seen = set()
        for migration in self.migrations:
            if migration.parent != previous or migration.revision in seen:
                raise ValueError("Migrations must form a unique ordered linear history")
            seen.add(migration.revision)
            previous = migration.revision
        if previous != self.revision:
            raise ValueError("Schema revision must be the last declared migration")
        return self


class PluginDefinition(Contract):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    api_version: Literal[1] = PROTOCOL_VERSION
    factory: str
    kind: Literal["business", "provider"] = "business"
    provider_kind: Literal["storage", "dashboard", "mcp", "llms"] | None = None
    config_schema: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "additionalProperties": False,
        }
    )
    requires: dict[str, Requirement] = Field(default_factory=dict)
    services: dict[str, ServiceSpec] = Field(default_factory=dict)
    resources: tuple[Resource, ...] = ()
    store: StoreSchema | None = None

    @model_validator(mode="after")
    def check_definition(self):
        if ":" not in self.factory:
            raise ValueError("Factory must be module:attribute")
        if (self.kind == "provider") != (self.provider_kind is not None):
            raise ValueError("Provider kind is required only for provider plugins")
        identities = [(item.kind, item.id) for item in self.resources]
        if len(identities) != len(set(identities)):
            raise ValueError("Duplicate owned resource")
        Draft202012Validator.check_schema(self.config_schema)
        return self


class OwnedResource(Contract):
    owner: str
    generation: str
    resource: Resource
    content: str | None = None

    @property
    def key(self) -> str:
        return f"{self.owner}/{self.resource.kind}/{self.resource.id}"


class PluginError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or {}

    def as_dict(self):
        return {"code": self.code, "message": self.message, "details": self.details}


def validate(schema: dict, value: Any) -> None:
    # JSON validation also rejects non-finite floats, even when a schema is open.
    json.dumps(value, allow_nan=False)
    Draft202012Validator(schema).validate(value)
