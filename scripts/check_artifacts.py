"""Check distribution metadata, SDK contents, typing marker and license files."""

import argparse
import tarfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path

from packaging.requirements import Requirement


def check_metadata(data: bytes, project: dict) -> None:
    metadata = BytesParser().parsebytes(data)
    expected = {
        "Name": project["name"],
        "Version": project["version"],
        "Requires-Python": project["requires-python"],
        "License-Expression": project["license"],
    }
    for key, value in expected.items():
        assert metadata[key] == value, (key, metadata[key], value)
    assert set(metadata.get_all("License-File", [])) == set(project["license-files"])
    assert {Requirement(value) for value in metadata.get_all("Requires-Dist", [])} == {
        Requirement(value) for value in project["dependencies"]
    }, "Runtime requirements differ from pyproject.toml"


def check_artifacts(dist_dir: Path, root: Path) -> None:
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    stem = f"{project['name'].replace('-', '_')}-{project['version']}"
    wheel_path = dist_dir / f"{stem}-py3-none-any.whl"
    sdist_path = dist_dir / f"{stem}.tar.gz"
    files = {path for path in dist_dir.iterdir() if not path.name.startswith(".")}
    assert files == {wheel_path, sdist_path}, f"Unexpected distributions: {files}"

    package = root / "src" / "toolframe_sdk"
    sources = {
        path.relative_to(root / "src").as_posix(): path.read_bytes()
        for path in package.rglob("*")
        if path.is_file() and (path.suffix == ".py" or path.name == "py.typed")
    }
    assert "toolframe_sdk/py.typed" in sources
    licenses = {
        name: (root / name).read_bytes() for name in project["license-files"]
    }
    dist_info = f"{stem}.dist-info"
    with zipfile.ZipFile(wheel_path) as wheel:
        check_metadata(wheel.read(f"{dist_info}/METADATA"), project)
        for name, content in sources.items():
            assert wheel.read(name) == content, f"Wheel source differs: {name}"
        for name, content in licenses.items():
            assert wheel.read(f"{dist_info}/licenses/{name}") == content, name
        assert all(
            name.startswith(("toolframe_sdk/", f"{dist_info}/"))
            for name in wheel.namelist()
        ), "Wheel includes files outside the SDK and its metadata"

    with tarfile.open(sdist_path) as sdist:
        def read(name: str) -> bytes:
            member = sdist.extractfile(f"{stem}/{name}")
            assert member is not None, f"Missing sdist file: {name}"
            with member:
                return member.read()

        check_metadata(read("PKG-INFO"), project)
        for name, content in sources.items():
            assert read(f"src/{name}") == content, f"Sdist source differs: {name}"
        for name, content in licenses.items():
            assert read(name) == content, name
        for name in ("pyproject.toml", "README.md"):
            assert read(name) == (root / name).read_bytes(), name
        forbidden = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "dist"}
        assert all(
            not forbidden.intersection(Path(name).parts)
            for name in sdist.getnames()
        ), "Sdist includes development environments or build output"

    print(f"Artifacts verified: {wheel_path.name}, {sdist_path.name}")
    print("SDK sources, py.typed, runtime requirements, LICENSE and NOTICE match.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist_dir", type=Path)
    args = parser.parse_args()
    check_artifacts(args.dist_dir.resolve(), Path(__file__).resolve().parents[1])


if __name__ == "__main__":
    main()
