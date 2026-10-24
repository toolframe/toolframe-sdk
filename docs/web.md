# Web contract v1

A business plugin can contribute a full HTML application without importing a
particular HTTP framework. Declare a `Resource(kind="web.app")` with a stable
lowercase ID, required business scopes, and this data:

```python
{
    "contract": 1,
    "entrypoint": "example.webapp:app",
    "package": "example",
    "assets": "web/assets.json",
    "access": "inherit",
}
```

`access="authenticated"` additionally requires a signed-in visitor. An optional
`expected_public_base` rejects mounts that do not match a prebuilt client router.
The resource's import package owns its templates and sealed asset manifest.

The factory returns a `WebApp` whose routes use unique IDs, canonical app-relative
paths and explicit methods. A `{parameter}` occupies one complete segment;
ambiguous path/method combinations and `/_tf` routes are rejected. A GET route
also supports HEAD. The provider handles OPTIONS after resource admission.

```python
from toolframe_sdk import WebApp, WebResponse, WebRoute

async def index(request, ctx):
    data = await ctx.call("example.notes@1/list")
    return WebResponse(str(len(data["notes"])), headers={"Content-Type": "text/plain"})

def app():
    return WebApp((WebRoute("index", "/", ("GET",), index),))
```

A Web factory and handlers execute inside the selected Web provider's Python
cohort, with the owner's `web` extra. Business services execute in their own
workers. The SDK imports no templating library, HTTP framework, CSS system or
browser library. Presentation helpers may be supplied by a provider package.

## Request and response

`WebRequest` contains method, app-relative path, query values, path parameters,
filtered headers and a bounded byte body. `json()` decodes JSON;
`await form()` returns fields and `WebUpload(filename, content_type, content)`
objects. Provider limits apply before a handler executes. Browser authentication
cookies and Core's internal forwarding headers are not exposed to handlers.

`WebResponse(body, status=200, headers={})` returns text or bytes. Header names and
values reject line breaks. The provider applies its cache, CSP and response-size
policy. Handler results are finite responses; WebSocket, streaming and ASGI
mount interfaces are not part of this contract.

`WebContext` is valid only during the request:

- `await ctx.call("service@major/method", params)` invokes a declared business
  service using the visitor's admitted identity. The owner must provide that
  service or declare it as a requirement. Internal/system methods are excluded.
- `ctx.url("route", params, query=...)` constructs a mount-aware route URL.
- `ctx.asset("logical/path.js")` constructs the selected immutable asset URL.
- `ctx.links` and `ctx.link("web.owner.resource")` expose authorized, available
  presentation entrances selected by Core.
- `ctx.form_fields()` emits escaped hidden CSRF/revision fields for unsafe forms.
  JSON clients send `ctx.csrf_token` as `X-CSRF-Token` and `ctx.revision` as
  `X-Web-Revision`. Neither value is a business credential.
- `ctx.presentation` describes the current browser access presentation. UI scope
  checks are conveniences; Core independently enforces all service permissions.

GET/HEAD requests can call only business methods marked `read_only=True`.
Resource, route and method scope requirements accumulate. A context cannot be
stored for background work, reused after a response or moved between resources,
workers or epochs. A domain call already admitted can finish after disconnect;
mutating services should use their own idempotency and concurrency controls.

## Publication and browser assets

Providers validate factories and return `WebAppCatalog` before publication. The
catalog contains routes, a sealed asset-set digest, exact asset paths, visibility,
CSP capabilities and optional SPA fallback. `resource_admit` binds a request to
that catalog before a handler executes; `resource_call` checks it again at each
business call. Core selects the public mount and generation.

Assets use `/_tf/assets/<digest>/<logical-path>` beneath the selected app mount.
Only declared assets may be addressed. Relative imports preserve their graph;
there is no framework global to all apps. Inherited assets use the current
resource policy, including before 304 responses. Explicit public assets must
contain no private material. A SPA fallback names an HTML asset and applies only
to unmatched HTML GET/HEAD requests inside that app, never reserved Core routes
or asset misses.

Default CSP accepts sealed same-origin resources. Apps can request supported
`script-src`, `style-src`, `img-src`, `font-src`, `connect-src` and `worker-src`
capabilities; wildcard and external origins are rejected. Explicit eval/WASM or
blob capabilities apply only to the declaring app. Trusted same-origin apps are
not isolated security principals.
