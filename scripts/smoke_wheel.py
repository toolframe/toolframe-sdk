"""Run outside the checkout with only the wheel and its runtime dependencies."""

import importlib
import importlib.metadata
import importlib.resources
import sys
import tomllib
from pathlib import Path

import jsonschema
import pydantic

import toolframe_sdk


def main() -> None:
    project = tomllib.loads(Path(sys.argv[1]).read_text())["project"]
    installed_version = importlib.metadata.version(project["name"])
    assert installed_version == project["version"] == toolframe_sdk.__version__
    installed_path = Path(toolframe_sdk.__file__).resolve()
    assert installed_path.is_relative_to(Path(sys.prefix).resolve()), installed_path

    for module in (
        "contracts",
        "http",
        "logging",
        "page",
        "rpc",
        "runtime",
        "worker",
        "tracing",
    ):
        importlib.import_module(f"toolframe_sdk.{module}")
    assert not any(name.startswith("opentelemetry") for name in sys.modules)
    assert importlib.util.find_spec("opentelemetry") is None, (
        "Base SDK must not install tracing dependencies"
    )
    assert importlib.resources.files(toolframe_sdk).joinpath("py.typed").is_file()

    definition = toolframe_sdk.PluginDefinition(
        id="smoke",
        factory="example:Plugin",
        authorization=toolframe_sdk.AuthorizationSpec(),
    )
    assert (
        toolframe_sdk.PluginDefinition.model_validate_json(definition.model_dump_json())
        == definition
    )
    toolframe_sdk.validate({"type": "integer"}, 1)
    try:
        toolframe_sdk.validate({"type": "integer"}, "invalid")
    except jsonschema.ValidationError:
        pass
    else:
        raise AssertionError("Installed JSON Schema validation did not reject bad data")

    print(f"Installed SDK {installed_version}: {installed_path}")
    print(
        f"Runtime dependencies: pydantic {pydantic.__version__}, "
        f"jsonschema {importlib.metadata.version('jsonschema')}"
    )
    print("All SDK modules, typed resources and contract validation passed.")


if __name__ == "__main__":
    main()
