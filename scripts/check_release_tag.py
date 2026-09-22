"""Reject branch dispatches and tags that differ from project.version."""

import argparse
import tomllib
from pathlib import Path


def check_release_tag(ref_type: str, ref_name: str, version: str) -> None:
    expected = f"v{version}"
    if ref_type != "tag" or ref_name != expected:
        raise ValueError(
            f"Publishing requires tag {expected!r}; got {ref_type} {ref_name!r}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ref_type")
    parser.add_argument("ref_name")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    try:
        check_release_tag(args.ref_type, args.ref_name, project["version"])
    except ValueError as error:
        parser.exit(1, f"{error}\n")
    print(f"Release tag verified: {args.ref_name}")


if __name__ == "__main__":
    main()
