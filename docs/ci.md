# CI and publishing

## Local validation

Install uv 0.12.8 and CPython 3.14.7, then run from this repository:

```sh
bash scripts/validate.sh
```

The same script runs in GitHub Actions. It creates a fresh temporary dev
environment and uv cache, installs the locked `dev` dependencies with
`uv sync --locked --group dev --no-sources`, runs Ruff and the complete pytest
suite, and builds with `uv build --no-sources`. uv builds the wheel from the newly
created sdist. The script then:

1. Runs `twine==7.0.0 check --strict` on both distributions in a separate environment.
2. Verifies package/version/Python/license metadata, runtime requirements, all SDK
   source files, `py.typed`, and the original LICENSE and NOTICE bytes in both archives.
3. Installs the wheel and its real runtime dependencies into another fresh
   environment, runs `uv pip check`, and checks every SDK module, version, typing
   marker, Pydantic contracts, JSON Schema validation and the worker CLI.

The installed-wheel checks run outside the source checkout using Python `-I`.
They do not install dev tools or optional provider HTTP libraries into that
environment. The SDK's optional ASGI helper imports without requiring uvicorn;
actually starting a provider HTTP server remains the provider's responsibility.
There are no sibling repositories or private dependency wheels to fetch.

The script does not use an existing `.venv` or `dist`, change `uv.lock`, or reuse a
populated wheelhouse. Temporary environments are removed on exit; validated
distributions are retained at the printed location only after all checks pass.
To choose their location, supply a directory that does not yet exist:

```sh
ci_output_parent="$(mktemp -d)"
bash scripts/validate.sh "$ci_output_parent/dist"
```

`SDK_CI_PYTHON` can select another installed Python 3.14 interpreter for local
diagnostics; CI always uses 3.14.7. Dependencies need network access to PyPI.
Runtime and development requirements remain governed by the existing project
metadata and lockfile; the wheel smoke test resolves public runtime requirements
as a new consumer would. The packaging tool is separately pinned in the script.
Twine 7.0.0 is required for the build backend's metadata version 2.5; Twine 6.2.0
rejects that metadata. See the [Twine changelog](https://twine.readthedocs.io/en/stable/changelog.html).

Workflow/script static checks:

```sh
actionlint .github/workflows/ci.yml .github/workflows/publish.yml
bash -n scripts/validate.sh
```

## GitHub Actions

`ci.yml` runs on pushes to `main`, pull requests, manual dispatch and reusable
workflow calls. Its Python 3.14.7 matrix uses `ubuntu-24.04` and `macos-15`; these
labels are listed in the [official GitHub runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).
Both jobs run the full local validation script and save wheel/sdist artifacts
named `sdk-distributions-<runner>-<commit SHA>` for seven days. Each checks out the
triggering SHA and has a 15-minute timeout. No job executes `pull_request_target`.

Actions are pinned to full commit IDs with release comments. Default permissions
are `contents: read`, checkout does not persist credentials, and uv is pinned to
0.12.8. The reusable workflow has no concurrency group that could cancel its
caller. See the [uv integration guide](https://docs.astral.sh/uv/guides/integration/github/)
for the setup and isolated build pattern.

## Publication

See [releasing](releasing.md) for dependency order, private repository access,
Trusted Publisher bindings, version tags and consumer installation checks.
