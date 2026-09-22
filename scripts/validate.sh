#!/usr/bin/env bash
set -euo pipefail

if [[ $# -gt 1 ]]; then
  printf 'Usage: bash scripts/validate.sh [new-artifact-directory]\n' >&2
  exit 2
fi

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="$(mktemp -d "${TMPDIR:-/tmp}/toolframe-sdk-ci.XXXXXX")"
trap 'rm -rf -- "$work_dir"' EXIT

artifact_dir="${1:-$(mktemp -d "${TMPDIR:-/tmp}/toolframe-sdk-artifacts.XXXXXX")/dist}"
if [[ -e "$artifact_dir" || -L "$artifact_dir" ]]; then
  printf 'Artifact directory must not already exist: %s\n' "$artifact_dir" >&2
  exit 2
fi
mkdir -p -- "$(dirname -- "$artifact_dir")"
artifact_dir="$(cd -- "$(dirname -- "$artifact_dir")" && pwd)/$(basename -- "$artifact_dir")"

unset VIRTUAL_ENV PYTHONHOME PYTHONPATH
export PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1
export UV_PROJECT_ENVIRONMENT="$work_dir/dev"
export UV_CACHE_DIR="$work_dir/uv-cache"
ci_python="${SDK_CI_PYTHON:-3.14.7}"
dev_python="$work_dir/dev/bin/python"

cd -- "$repo_root"
printf 'Fresh validation workspace: %s\n' "$work_dir"
uv --version
uv sync --locked --group dev --no-sources --python "$ci_python"
"$dev_python" --version
"$dev_python" -m ruff check --no-cache src tests scripts
"$dev_python" -m pytest -q -p no:cacheprovider --basetemp "$work_dir/pytest"

# With neither --wheel nor --sdist, uv builds an sdist then a wheel from it.
uv build --no-sources --python "$ci_python" --out-dir "$work_dir/dist"
uv venv --python "$ci_python" "$work_dir/packaging"
uv pip install --python "$work_dir/packaging/bin/python" 'twine==7.0.0'
"$work_dir/packaging/bin/python" -m twine check --strict "$work_dir/dist/"*
"$work_dir/packaging/bin/python" scripts/check_artifacts.py "$work_dir/dist"

uv venv --python "$ci_python" "$work_dir/wheel"
uv pip install --python "$work_dir/wheel/bin/python" "$work_dir/dist/"*.whl
uv pip check --python "$work_dir/wheel/bin/python"
(
  cd -- "$work_dir"
  "$work_dir/wheel/bin/python" -I "$repo_root/scripts/smoke_wheel.py" \
    "$repo_root/pyproject.toml"
  "$work_dir/wheel/bin/python" -I -m toolframe_sdk.worker --help
)

# Export only after every check succeeds, without reusing or deleting old output.
mkdir -- "$artifact_dir"
cp -- "$work_dir/dist/"*.whl "$work_dir/dist/"*.tar.gz "$artifact_dir/"
printf 'Validation passed. Distributions: %s\n' "$artifact_dir"
