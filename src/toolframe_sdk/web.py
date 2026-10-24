"""Framework-independent Web app declarations and request-scoped service access."""

from __future__ import annotations

import html
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import quote, urlencode

from pydantic import Field, field_validator, model_validator

from .contracts import Contract, PluginError

WEB_CONTRACT = 1
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_PARAMETER = re.compile(r"\{([a-z][a-z0-9_]*)\}")


def relative_file(value: str) -> str:
    """Validate an exact logical path, before URL decoding or filesystem access."""
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or str(path) != value
        or any(part in {".", "..", ""} for part in value.split("/"))
        or any(character in value for character in "\\%?#:\x00")
        or any(ord(character) < 32 or character.isspace() for character in value)
    ):
        raise ValueError("File path must be a canonical package-relative path")
    return value


def public_base(value: str) -> str:
    if value == "/":
        return value
    if not value.startswith("/") or value.endswith("/"):
        raise ValueError("Public base must be a canonical absolute path")
    relative_file(value[1:])
    return value


def join_base(base: str, path: str = "/") -> str:
    public_base(base)
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError("App path must start with one slash")
    return base.rstrip("/") + path


class WebResourceData(Contract):
    contract: Literal[1]
    entrypoint: str
    package: str
    assets: str
    access: Literal["inherit", "authenticated"] = "inherit"
    expected_public_base: str | None = None

    @field_validator("entrypoint")
    @classmethod
    def entry_reference(cls, value):
        if not re.fullmatch(r"[a-zA-Z]\w*(?:\.\w+)*:[a-zA-Z]\w*", value):
            raise ValueError("Web entrypoint must be module:factory")
        return value

    @field_validator("package")
    @classmethod
    def package_name(cls, value):
        if not re.fullmatch(r"[a-zA-Z]\w*(?:\.\w+)*", value):
            raise ValueError("Web package must be an import package")
        return value

    @field_validator("assets")
    @classmethod
    def asset_path(cls, value):
        return relative_file(value)

    @field_validator("expected_public_base")
    @classmethod
    def expected_base(cls, value):
        return public_base(value) if value is not None else None


class WebRouteSpec(Contract):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    path: str
    methods: tuple[Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"], ...]
    required_scopes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_route(self):
        if not self.methods or len(set(self.methods)) != len(self.methods):
            raise ValueError("Route requires distinct methods")
        if not self.path.startswith("/") or self.path.startswith("//"):
            raise ValueError("Route path must be app-relative with one leading slash")
        if self.path == "/_tf" or self.path.startswith("/_tf/"):
            raise ValueError("The /_tf path is reserved for Web assets")
        parts = self.path[1:].split("/") if self.path != "/" else []
        names = []
        for position, part in enumerate(parts):
            parameter = _PARAMETER.fullmatch(part)
            if parameter:
                names.append(parameter[1])
            elif not part and position == len(parts) - 1:
                continue
            else:
                relative_file(part)
                if "{" in part or "}" in part:
                    raise ValueError("Parameters must occupy a complete path segment")
        if len(names) != len(set(names)):
            raise ValueError("Route parameter names must be unique")
        return self

    @property
    def allowed_methods(self):
        return set(self.methods) | ({"HEAD"} if "GET" in self.methods else set())

    def match(self, path: str) -> dict[str, str] | None:
        parts = self.path.split("/")
        incoming = path.split("/")
        if len(parts) != len(incoming):
            return None
        result = {}
        for expected, actual in zip(parts, incoming, strict=True):
            parameter = _PARAMETER.fullmatch(expected)
            if parameter:
                if (
                    not actual
                    or actual in {".", ".."}
                    or any(character in actual for character in "\\%\x00")
                ):
                    return None
                result[parameter[1]] = actual
            elif expected != actual:
                return None
        return result

    def url(self, params=None):
        params = params or {}
        names = set(_PARAMETER.findall(self.path))
        if names != set(params):
            raise PluginError("invalid_request", "Route parameters do not match")
        for value in params.values():
            value = str(value)
            if not value or value in {".", ".."} or any(c in value for c in "/\\%\x00"):
                raise PluginError("invalid_request", "Invalid route parameter")
        return _PARAMETER.sub(lambda m: quote(str(params[m[1]]), safe=""), self.path)


def validate_routes(routes):
    if not routes or len(routes) > 256:
        raise ValueError("A Web app requires 1–256 routes")
    seen = set()
    for index, route in enumerate(routes):
        if route.id in seen:
            raise ValueError("Duplicate Web route id")
        seen.add(route.id)
        for previous in routes[:index]:
            left, right = route.path.split("/"), previous.path.split("/")
            overlap = len(left) == len(right) and all(
                a == b
                or (
                    bool(a)
                    and bool(b)
                    and (_PARAMETER.fullmatch(a) or _PARAMETER.fullmatch(b))
                )
                for a, b in zip(left, right, strict=True)
            )
            if overlap and route.allowed_methods & previous.allowed_methods:
                raise ValueError("Ambiguous Web route/method combination")


class WebAppCatalog(Contract):
    """Prepared public policy, returned by the provider before publication."""

    contract: Literal[1] = 1
    routes: tuple[WebRouteSpec, ...]
    asset_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    asset_paths: tuple[str, ...]
    asset_visibility: Literal["inherit", "public"] = "inherit"
    csp: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    spa_fallback: str | None = None

    @model_validator(mode="after")
    def validate_catalog(self):
        validate_routes(self.routes)
        if len(self.asset_paths) > 2048 or len(set(self.asset_paths)) != len(
            self.asset_paths
        ):
            raise ValueError("Asset paths must be distinct and bounded")
        for path in self.asset_paths:
            relative_file(path)
        if len({path.casefold() for path in self.asset_paths}) != len(self.asset_paths):
            raise ValueError("Asset paths must not collide on case-insensitive systems")
        allowed = {
            "script-src",
            "style-src",
            "img-src",
            "font-src",
            "connect-src",
            "worker-src",
        }
        for directive, sources in self.csp.items():
            if directive not in allowed or not sources:
                raise ValueError("Unsupported CSP directive")
            for source in sources:
                if source not in {
                    "'self'",
                    "'none'",
                    "'unsafe-eval'",
                    "'wasm-unsafe-eval'",
                    "data:",
                    "blob:",
                }:
                    raise ValueError(
                        "Web CSP sources must be explicit supported capabilities"
                    )
            if "'none'" in sources and len(sources) != 1:
                raise ValueError("CSP 'none' cannot be combined with other sources")
        if self.spa_fallback is not None and self.spa_fallback not in self.asset_paths:
            raise ValueError("SPA fallback must be a declared public file")
        return self


@dataclass(frozen=True)
class WebRoute:
    id: str
    path: str
    methods: tuple[str, ...]
    handler: Callable
    required_scopes: tuple[str, ...] = ()

    def __post_init__(self):
        self.spec()
        if not callable(self.handler):
            raise TypeError("Route handler must be callable")

    def spec(self):
        return WebRouteSpec(
            id=self.id,
            path=self.path,
            methods=self.methods,
            required_scopes=self.required_scopes,
        )


@dataclass(frozen=True)
class WebApp:
    routes: tuple[WebRoute, ...]
    templates: str = "web/templates"
    csp: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    spa_fallback: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "routes", tuple(self.routes))
        object.__setattr__(
            self, "csp", MappingProxyType({k: tuple(v) for k, v in self.csp.items()})
        )
        relative_file(self.templates)
        validate_routes(tuple(route.spec() for route in self.routes))


@dataclass(frozen=True)
class WebUpload:
    filename: str
    content_type: str
    content: bytes


@dataclass(frozen=True)
class WebRequest:
    method: str
    path: str
    query: Mapping[str, tuple[str, ...]]
    path_params: Mapping[str, str]
    headers: Mapping[str, str]
    body: bytes
    _form: Callable[[], Awaitable[Mapping[str, Any]]]

    async def form(self):
        return await self._form()

    def json(self):
        import json

        try:
            return json.loads(self.body)
        except (ValueError, UnicodeError) as error:
            raise PluginError(
                "invalid_request", "Request body is not valid JSON"
            ) from error


@dataclass(frozen=True)
class WebResponse:
    body: str | bytes = b""
    status: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.body, (str, bytes)) or not 200 <= self.status <= 599:
            raise ValueError("Invalid Web response body or status")
        if any(
            any(c in str(k) + str(v) for c in "\r\n\x00")
            for k, v in self.headers.items()
        ):
            raise ValueError("Response headers cannot contain line breaks")
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))


class WebContext:
    """One admitted HTTP request; never a background or machine credential."""

    def __init__(self, runtime, resource, epoch, reference, admission, catalog):
        self.owner = resource.owner
        self.generation = resource.generation
        self.presentation = MappingProxyType(admission["presentation"])
        self.revision = admission["revision"]
        self.csrf_token = admission["csrf"]
        self.public_base = admission["public_base"]
        self.links = MappingProxyType(admission.get("links", {}))
        self.log = runtime.log.bind(**{"toolframe.web.owner": self.owner})
        self._runtime, self._resource = runtime, resource
        self._epoch, self._reference = epoch, reference
        self._catalog, self._closed = catalog, False

    def close(self):
        self._closed = True

    async def call(self, method, params=None, *, timeout=30):
        if self._closed:
            raise PluginError("web_context_closed", "Web request has ended")
        return await self._runtime.resource_call(
            self._resource.key,
            method,
            params,
            epoch=self._epoch,
            access_context=self._reference,
            timeout=timeout,
        )

    def url(self, route_id, params=None, query=None):
        route = next((r for r in self._catalog.routes if r.id == route_id), None)
        if route is None:
            raise PluginError("web_route_missing", "Unknown Web route")
        path = join_base(self.public_base, route.url(params))
        return path + ("?" + urlencode(query, doseq=True) if query else "")

    def asset(self, path):
        if path not in self._catalog.asset_paths:
            raise PluginError("web_asset_missing", "Undeclared Web asset")
        return join_base(
            self.public_base,
            f"/_tf/assets/{self._catalog.asset_digest}/{quote(path, safe='/')}",
        )

    def link(self, target):
        try:
            return self.links[target]
        except KeyError as error:
            raise PluginError(
                "web_target_unavailable", "Target is not available for navigation"
            ) from error

    def form_fields(self):
        return "".join(
            f'<input type="hidden" name="{name}" value="{html.escape(value, quote=True)}">'
            for name, value in (
                ("_tf_csrf", self.csrf_token),
                ("_tf_revision", self.revision),
            )
        )
