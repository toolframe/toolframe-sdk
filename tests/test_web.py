import pytest
from pydantic import ValidationError

from toolframe_sdk import (
    AuthorizationSpec,
    PluginDefinition,
    PluginError,
    Resource,
    WebApp,
    WebResponse,
    WebRoute,
)
from toolframe_sdk.web import WebAppCatalog, WebRouteSpec, join_base, relative_file


def test_web_resource_round_trip_and_explicit_contract():
    resource = Resource(
        id="main",
        kind="web.app",
        data={
            "contract": 1,
            "entrypoint": "notes.web:app",
            "package": "notes",
            "assets": "web/assets.json",
        },
    )
    definition = PluginDefinition(
        id="notes",
        factory="notes:Plugin",
        authorization=AuthorizationSpec(),
        resources=(resource,),
    )
    assert (
        PluginDefinition.model_validate_json(definition.model_dump_json()) == definition
    )
    with pytest.raises(ValidationError):
        Resource(id="main", kind="web.app", data={**resource.data, "contract": 2})
    with pytest.raises(ValidationError):
        Resource(
            id="main", kind="web.app", data={**resource.data, "assets": "../secret"}
        )


@pytest.mark.parametrize(
    "path",
    [
        "../secret",
        "a/../secret",
        "a//b",
        "./a",
        "/a",
        "a%2fb",
        "a\\b",
        "a?x",
        "a#x",
        "a b",
    ],
)
def test_noncanonical_asset_paths_are_rejected(path):
    with pytest.raises(ValueError):
        relative_file(path)


def test_route_matching_and_encoded_url_parameters_share_contract():
    route = WebRouteSpec(id="item", path="/notes/{id}", methods=("GET",))
    assert route.match("/notes/香港") == {"id": "香港"}
    assert route.url({"id": "a b"}) == "/notes/a%20b"
    assert route.allowed_methods == {"GET", "HEAD"}
    assert route.match("/notes/..") is None
    assert route.match("/notes/a/b") is None
    with pytest.raises(PluginError):
        route.url({"id": "../secret"})
    assert join_base("/notes", "/items/one") == "/notes/items/one"
    assert join_base("/", "/") == "/"


def test_ambiguous_routes_fail_before_publication():
    async def handler(request, context):
        return WebResponse("ok")

    with pytest.raises(ValueError, match="Ambiguous"):
        WebApp(
            (
                WebRoute("item", "/{id}", ("GET",), handler),
                WebRoute("new", "/new", ("HEAD",), handler),
            )
        )
    WebApp(
        (
            WebRoute("get", "/item", ("GET",), handler),
            WebRoute("post", "/item", ("POST",), handler),
        )
    )
    with pytest.raises(ValueError, match="reserved"):
        WebRoute("bad", "/_tf/assets", ("GET",), handler)


def test_catalog_rejects_unbounded_csp_and_asset_confusion():
    values = {
        "routes": [{"id": "index", "path": "/", "methods": ["GET"]}],
        "asset_digest": "a" * 64,
        "asset_paths": ["app.js"],
    }
    WebAppCatalog(**values, csp={"worker-src": ("'self'",)})
    for csp in (
        {"script-src": ("*",)},
        {"default-src": ("'self'",)},
        {"script-src": ("https://unsealed.example",)},
        {"script-src": ("'none'", "'self'")},
    ):
        with pytest.raises(ValueError):
            WebAppCatalog(**values, csp=csp)
    with pytest.raises(ValueError):
        WebAppCatalog(**{**values, "asset_paths": ["a.js", "A.js"]})
    with pytest.raises(ValueError):
        WebResponse("ok", headers={"Location": "/\r\nSet-Cookie: bypass"})


@pytest.mark.parametrize("identifier", ["with.dot", "with/slash", "", "Uppercase"])
def test_web_resource_id_can_always_form_a_gateway_target(identifier):
    with pytest.raises(ValidationError):
        Resource(
            id=identifier,
            kind="web.app",
            data={
                "contract": 1,
                "entrypoint": "example:app",
                "package": "example",
                "assets": "web/assets.json",
            },
        )
