# Plugin compatibility

Core, SDK and plugins have independent versions. Starting with SDK 0.4, later
0.x SDK releases preserve the documented plugin API and worker protocol.
Additive features may raise the minimum SDK version of plugins that use them;
they do not require existing plugins to change their versions or metadata.
Deprecated public interfaces remain usable throughout the supported 0.x series.
An incompatible change needs a compatibility implementation or an explicitly
announced migration boundary; a new Core minor version alone is not that boundary.
This is a Toolframe policy, not a guarantee supplied by the `0.x` version number.

Declare the lowest SDK that implements the APIs and fixes the plugin needs:

```toml
dependencies = ["toolframe-sdk>=0.4.0,<1"]
```

Plugins using the Web contracts introduced in SDK 0.4.1 use `>=0.4.1,<1`.
The upper bound excludes a future 1.0 release; it does not require upgrading any
package to 1.0. A minimum must be verified, not inferred from a successful test
against a newer SDK. Security fixes can require a higher minimum. Private
attributes, undocumented behavior and explicitly experimental APIs are outside
this compatibility promise. Preserve authorization and validation boundaries when
adding compatibility behavior; compatibility never means bypassing a rejected
scope, invalid configuration or unsupported protocol.

Package requirements describe the supported range. Deployment locks and release
manifests still select exact versions and artifact hashes. Core may select an
exact SDK for its own runtime. An independent plugin must not pin that same
patch merely because Core selected it. Previously published wheel metadata is
immutable: changing a pin requires one new plugin distribution, after which
compatible updates can reuse that wheel.

## Verification

`scripts/check_plugin_compatibility.py` builds one wheel and installs it into
separate environments using the declared minimum SDK and the newest SDK allowed
by the range. Each environment checks installed metadata, imports the public
entry point, describes a real SDK worker and runs the package tests. The newest
SDK is selected before test dependencies are added and remains constrained, so
test dependencies cannot silently downgrade the SDK under test. No editable
sibling sources or development lockfiles participate.

Run it from an SDK checkout with Python 3.14 and uv:

```sh
uv run --no-project --with 'packaging>=25' python \
  scripts/check_plugin_compatibility.py \
  --project /path/to/plugin --mode minimum --output build/compat-minimum
uv run --no-project --with 'packaging>=25' python \
  scripts/check_plugin_compatibility.py \
  --project /path/to/plugin --mode latest --output build/compat-latest
```

Outputs must be new directories. `--wheel` reuses an already built plugin wheel;
`--sdk-wheel` selects an explicit SDK candidate for release qualification.
`--dependency-wheel` supplies explicit test dependency wheels before those
dependencies are published. Every supplied wheel and resolved package is recorded
in the report. Ordinary checks use index packages. Optional external services and
opt-in Core integration tests still require their owning validation workflows;
skips are visible in the pytest output and are not application acceptance.

Plugin CI runs both endpoints on pull requests, main and a daily schedule.
The SDK's composite action runs the same script; callers pin its Git commit.
Keep the existing locked validation as the reproducible baseline. A green locked
run does not replace the latest-dependency check. A newly incompatible dependency
requires a fix or a documented, targeted exclusion, not suppressing the check.

SDK distribution validation builds the frozen 0.4 consumer once, then installs
and invokes that exact wheel with SDK 0.4.0 and the candidate SDK wheel. The
fixture source and package metadata stay frozen as later SDKs are qualified.
Core release qualification additionally uses retained plugin wheels without
rebuilding their code. Core owns installed application, authoring,
reload and data-retention checks. Protocol numbers identify wire contracts,
not package releases: compatible package updates do not increment the protocol.
