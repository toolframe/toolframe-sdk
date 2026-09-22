"""Release guards must fail before the publishing job can receive an OIDC token."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "check_release_tag",
    Path(__file__).resolve().parents[1] / "scripts" / "check_release_tag.py",
)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def test_release_tag_matches_project_version():
    release.check_release_tag("tag", "v0.2.0", "0.2.0")


@pytest.mark.parametrize(
    ("ref_type", "ref_name"),
    [
        ("branch", "main"),
        ("branch", "v0.2.0"),
        ("tag", "v0.1.0"),
        ("tag", "0.2.0"),
        ("tag", "v0.2.0rc1"),
    ],
)
def test_release_tag_rejects_non_release_refs(ref_type, ref_name):
    with pytest.raises(ValueError, match="Publishing requires tag"):
        release.check_release_tag(ref_type, ref_name, "0.2.0")
