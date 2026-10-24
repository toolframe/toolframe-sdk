"""Generic identity rendering does not bake in presentation types."""

import importlib
import sys
import zipfile
from pathlib import Path

import pytest

from toolframe_sdk.authoring import plugin_names, render_plugin, write_template


def template(archive):
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(
            "example/pyproject.toml",
            """[project]
name = "toolframe-example"
[project.entry-points."toolframe.plugins"]
example = "toolframe_example.plugin:plugin"
[tool.hatch.build.targets.wheel]
packages = ["src/toolframe_example"]
""",
        )
        bundle.writestr("example/LICENSE", "Preserve the original license verbatim.\n")
        bundle.writestr(
            "example/src/toolframe_example/plugin.py",
            """import toolframe_example.service
IDENTITY = "example"
SERVICE = "example.echo"
PROSE = "About example and toolframe_example"
""",
        )
        bundle.writestr(
            "example/README.md", "Use `example.echo`, not prose about example.\n"
        )
        bundle.writestr("example/example.toml", 'package = "toolframe_example"\n')


def test_packaged_traversable_identity_and_licenses(tmp_path):
    path = tmp_path / "template.zip"
    template(path)
    with zipfile.ZipFile(path) as bundle:
        rendered = render_plugin(
            zipfile.Path(bundle, "example/"), "new-plugin", text_suffixes=(".toml",)
        )
    assert rendered[Path("LICENSE")] == b"Preserve the original license verbatim.\n"
    source = rendered[Path("src/toolframe_new_plugin/plugin.py")].decode()
    assert "import toolframe_new_plugin.service" in source
    assert "new-plugin.echo" in source
    assert "About example and toolframe_example" in source
    assert (
        rendered[Path("README.md")]
        == b"Use `new-plugin.echo`, not prose about example.\n"
    )
    assert "toolframe_new_plugin" in rendered[Path("example.toml")].decode()
    output = tmp_path / "output"
    output.mkdir()
    write_template(output, rendered)
    assert (output / "LICENSE").read_bytes() == rendered[Path("LICENSE")]
    with pytest.raises(FileExistsError):
        write_template(output, rendered)


def test_names_and_path_boundaries(tmp_path):
    assert plugin_names("a--b-") == ("toolframe-a--b-plugin", "toolframe_a__b_plugin")
    for name in ("Bad", "../other", "", "a" * 65):
        with pytest.raises(ValueError):
            plugin_names(name)
    with pytest.raises(ValueError, match="escapes"):
        write_template(tmp_path, {Path("../outside"): b"x"})


def test_importing_helper_does_not_import_renderer_dependency():
    # tomlkit is lazy: runtime-only SDK installs need no authoring dependency.
    sys.modules.pop("toolframe_sdk.authoring", None)
    before = "tomlkit" in sys.modules
    importlib.import_module("toolframe_sdk.authoring")
    assert ("tomlkit" in sys.modules) == before
