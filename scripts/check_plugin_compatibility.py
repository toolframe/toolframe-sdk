"""Verify one immutable plugin wheel against a minimum or current SDK."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version


def sdk_requirement(project):
    matches = [
        Requirement(value)
        for value in project["dependencies"]
        if canonicalize_name(Requirement(value).name) == "toolframe-sdk"
    ]
    if len(matches) != 1 or matches[0].url or matches[0].marker:
        raise ValueError("Declare one unconditional SDK version range")
    requirement = matches[0]
    floors = [
        Version(item.version) for item in requirement.specifier if item.operator == ">="
    ]
    if len(floors) != 1 or floors[0] not in requirement.specifier:
        raise ValueError("The SDK range needs one inclusive, installable minimum")
    if any(item.operator == "==" for item in requirement.specifier):
        raise ValueError("Independent plugins must not pin an SDK patch")
    if not any(
        item.operator == "<" and Version(item.version) == Version("1.0")
        for item in requirement.specifier
    ):
        raise ValueError("The supported SDK range must declare <1")
    return str(requirement), str(floors[0])


def development_requirements(document):
    groups = document.get("dependency-groups", {})

    def expand(name, parents=()):
        if name in parents:
            raise ValueError("Dependency groups must not contain a cycle")
        result = []
        for item in groups.get(name, []):
            if isinstance(item, str):
                result.append(item)
            elif isinstance(item, dict) and set(item) == {"include-group"}:
                result.extend(expand(item["include-group"], (*parents, name)))
            else:
                raise ValueError(f"Unsupported dependency group entry: {item!r}")
        return result

    # Core has its own interpreter and pins its own SDK. Its opt-in application
    # tests run separately; installing it here would constrain the worker SDK.
    requirements = [
        value
        for value in expand("dev")
        if canonicalize_name(Requirement(value).name) != "toolframe-core"
    ]
    settings = document.get("tool", {}).get("toolframe-compatibility", {})
    return [*requirements, *settings.get("test-dependencies", [])]


SMOKE = r"""
import asyncio, importlib, importlib.metadata as metadata, json, pathlib, sys
from packaging.requirements import Requirement
from toolframe_sdk import PluginDefinition, PROTOCOL_VERSION
from toolframe_sdk.rpc import RpcPeer
name, expected_sdk = sys.argv[1:]
assert metadata.version("toolframe-sdk") == expected_sdk
distribution = metadata.distribution(name)
direct = json.loads(distribution.read_text("direct_url.json") or "{}")
assert not direct.get("dir_info", {}).get("editable", False)
plugin_entries = [entry for entry in distribution.entry_points if entry.group == "toolframe.plugins"]
module_name = plugin_entries[0].value.split(":")[0] if plugin_entries else name.replace("-", "_")
module = importlib.import_module(module_name)
assert pathlib.Path(module.__file__).resolve().is_relative_to(pathlib.Path(sys.prefix).resolve())
requirement = next(Requirement(value) for value in distribution.requires or []
                   if Requirement(value).name == "toolframe-sdk")
assert expected_sdk in requirement.specifier
entries = []
for entry in distribution.entry_points:
    if entry.group == "toolframe.plugins":
        definition = entry.load()
        PluginDefinition.model_validate(definition.model_dump(mode="json"))
        entries.append({"id": definition.id, "entrypoint": entry.value})
async def describe(entry):
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-m", "toolframe_sdk.worker", "--entrypoint", entry["entrypoint"],
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    async def unavailable(method, params):
        raise RuntimeError("Preflight must not call application services")
    peer = RpcPeer(process.stdout, process.stdin, unavailable)
    reader = asyncio.create_task(peer.run())
    try:
        report = await asyncio.wait_for(peer.request("runtime.describe"), 30)
        assert report["definition"]["id"] == entry["id"]
        assert report["protocol"] == PROTOCOL_VERSION
    finally:
        await peer.close()
        await asyncio.gather(reader, return_exceptions=True)
        try:
            await asyncio.wait_for(process.wait(), 10)
        except TimeoutError:
            process.kill()
            await process.wait()
for entry in entries:
    asyncio.run(describe(entry))
print(json.dumps({"sdk": expected_sdk, "protocol": PROTOCOL_VERSION, "entries": entries}))
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--mode", choices=("minimum", "latest"), required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--sdk-wheel", type=Path)
    parser.add_argument("--dependency-wheel", type=Path, action="append", default=[])
    args = parser.parse_args()
    project_root = args.project.resolve()
    output = args.output.resolve()
    if output.exists():
        parser.error("--output must not exist; prior evidence is never overwritten")
    document = tomllib.loads((project_root / "pyproject.toml").read_text())
    project = document["project"]
    requirement, minimum = sdk_requirement(project)
    output.mkdir(parents=True)
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {"VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "UV_PROJECT_ENVIRONMENT"}
    }
    env.update(PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
    report = {
        "package": project["name"],
        "mode": args.mode,
        "minimum": minimum,
        "requirement": requirement,
        "passed": False,
        "commands": [],
    }

    def run(*command, cwd=project_root, capture=False):
        argv = list(map(str, command))
        print("+ " + shlex.join(argv), flush=True)
        report["commands"].append(argv)
        return subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            check=True,
            text=True,
            stdout=subprocess.PIPE if capture else None,
        )

    try:
        wheel = args.wheel.resolve() if args.wheel else None
        if wheel is None:
            run(
                "uv",
                "build",
                "--no-sources",
                "--wheel",
                "--out-dir",
                output / "dist",
                project_root,
            )
            (wheel,) = (output / "dist").glob("*.whl")
        report["wheel"] = {
            "path": str(wheel),
            "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        }
        runtime = output / "runtime"
        run("uv", "venv", "--python", sys.executable, runtime)
        python = runtime / "bin/python"
        selected = (
            f"toolframe-sdk=={minimum}" if args.mode == "minimum" else requirement
        )
        sdk = [selected, *([str(args.sdk_wheel.resolve())] if args.sdk_wheel else [])]
        run(
            "uv",
            "pip",
            "install",
            "--no-sources",
            "--python",
            python,
            "--upgrade",
            wheel,
            *sdk,
            "packaging>=25",
        )
        sdk_version = run(
            python,
            "-I",
            "-c",
            "from importlib.metadata import version; print(version('toolframe-sdk'))",
            capture=True,
        ).stdout.strip()
        constraint = output / "sdk-constraint.txt"
        constraint.write_text(f"toolframe-sdk=={sdk_version}\n")
        report["sdk"] = sdk_version
        if args.sdk_wheel:
            report["sdk_wheel"] = {
                "path": str(args.sdk_wheel.resolve()),
                "sha256": hashlib.sha256(args.sdk_wheel.read_bytes()).hexdigest(),
            }
        smoke = json.loads(
            run(
                python,
                "-I",
                "-c",
                SMOKE,
                project["name"],
                sdk_version,
                cwd=output,
                capture=True,
            ).stdout
        )
        report["smoke"] = smoke
        requirements = development_requirements(document)
        extras = sorted(
            set(project.get("optional-dependencies", {})) - {"operator", "tracing"}
        )
        tested_wheel = str(wheel) + ("[" + ",".join(extras) + "]" if extras else "")
        dependency_wheels = [path.resolve() for path in args.dependency_wheel]
        report["dependency_wheels"] = [
            {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in dependency_wheels
        ]
        run(
            "uv",
            "pip",
            "install",
            "--no-sources",
            "--python",
            python,
            "--constraint",
            constraint,
            tested_wheel,
            *requirements,
            *dependency_wheels,
            "pytest>=8,<10",
        )
        run("uv", "pip", "check", "--python", python)
        tests = [
            path
            for pattern in ("test_*.py", "*_test.py")
            for path in (project_root / "tests").rglob(pattern)
        ]
        report["package_tests"] = bool(tests)
        if tests:
            run(
                python,
                "-I",
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "--junitxml",
                output / "pytest.xml",
                "tests",
            )
        report["resolved"] = json.loads(
            run(
                "uv",
                "pip",
                "list",
                "--python",
                python,
                "--format",
                "json",
                capture=True,
            ).stdout
        )
        report["passed"] = True
    finally:
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
