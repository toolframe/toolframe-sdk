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

`ci.yml` runs on every push, pull request, manual dispatch and reusable-workflow
call. Its Python 3.14.7 matrix uses `ubuntu-24.04` and `macos-15`; these labels are
listed in the [official GitHub runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).
Both jobs run the full local validation script and save wheel/sdist artifacts
named `sdk-distributions-<runner>-<commit SHA>` for seven days. Each checks out the
triggering SHA and has a 15-minute timeout. No job executes `pull_request_target`.

Actions are pinned to full commit IDs with release comments. Default permissions
are `contents: read`, checkout does not persist credentials, and uv is pinned to
0.12.8. The reusable workflow has no concurrency group that could cancel its
caller. See the [uv integration guide](https://docs.astral.sh/uv/guides/integration/github/)
for the setup and isolated build pattern.

## Manual publishing

`publish.yml` supports only manual dispatch. Select `repository=testpypi`
(the default) or `repository=pypi` and run it **on an existing tag** exactly equal
to `v<project.version>`. For the current version, that is `v0.3.0`.
A branch named `v0.3.0`, a plain `0.3.0` tag or a different version all fail the
release guard before validation and upload.

The workflow checks out the dispatch SHA, validates the tag against
`pyproject.toml`, and calls `ci.yml` at that same revision. Publishing waits for
both matrix jobs to pass, then downloads the Ubuntu distribution artifact from
that same run. It never rebuilds in the publishing job or takes artifacts from a
different run. Re-running a release still requires the complete validation gate.

Only the publishing job has `id-token: write`. It runs in the explicitly selected
`testpypi` or `pypi` environment, uses the pinned PyPA publishing action and the
matching fixed upload endpoint, and does not check out repository code.
Uploads to the same index are serialized without cancelling an active upload.
Existing versions fail normally; duplicate uploads are not silently skipped.
The OIDC job is in the top-level workflow, as required by the
[PyPA publishing action](https://github.com/pypa/gh-action-pypi-publish).

Required external setup before a future upload:

1. Choose the actual GitHub owner and repository, configure its remote, push the
   reviewed commit, and enable Actions. `publish.yml` must exist on the default
   branch and on the tagged revision. No GitHub owner or remote is assumed here.
2. Create GitHub environments named exactly `testpypi` and `pypi`. Configure
   permitted version tags and any required reviewers, especially for production.
   Protect release tags from unauthorized creation or movement.
3. Configure a Trusted Publisher separately on TestPyPI and PyPI, using the actual
   owner, actual repository name, workflow filename **`publish.yml`**, and the
   corresponding environment **`testpypi`** or **`pypi`**. The distribution/project
   name is **`toolframe-sdk`**. For an existing project use its Publishing settings;
   for a new project configure a pending publisher under the account. No API-token
   secrets are needed. Follow the official
   [existing-project](https://docs.pypi.org/trusted-publishers/adding-a-publisher/)
   or [pending-publisher](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)
   instructions; a pending publisher does not reserve the package name.
4. After reviewing the validated revision, create and push the matching version
   tag. Dispatch TestPyPI first, then explicitly select PyPI for production.
   For example, once the remote and tag exist, the future dispatch commands are:

   ```sh
   gh workflow run publish.yml --ref v0.3.0 -f repository=testpypi
   gh workflow run publish.yml --ref v0.3.0 -f repository=pypi
   ```

Creating these files and running local checks does not create repositories,
register publishers, push tags or upload packages. Linux SDK CI does not establish
support for the full Toolframe application. Hosted runners, Windows, physical
Intel Macs and a real OIDC upload must not be claimed as locally verified.
