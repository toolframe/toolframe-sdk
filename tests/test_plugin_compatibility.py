"""Exercise SDK selection without a resolver silently changing the target."""

import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "plugin_compatibility",
    Path(__file__).resolve().parents[1] / "scripts/check_plugin_compatibility.py",
)
compatibility = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compatibility)


@pytest.mark.parametrize("minimum", ["0.4.0", "0.4.1"])
def test_minimum_is_derived_from_the_consumer_contract(minimum):
    requirement, actual = compatibility.sdk_requirement(
        {"dependencies": ["pillow>=12", f"toolframe-sdk>={minimum},<1"]}
    )
    assert actual == minimum
    assert "0.8.0" in compatibility.Requirement(requirement).specifier
    assert "1.0.0" not in compatibility.Requirement(requirement).specifier


@pytest.mark.parametrize(
    "requirement",
    [
        "toolframe-sdk==0.4.1",
        "toolframe-sdk>=0.4.1",
        "toolframe-sdk>=0.4.1,!=1.0",
        "toolframe-sdk>0.4.1,<1",
        "toolframe-sdk>=0.4.1,<1,!=0.4.1",
        "toolframe-sdk>=0.4.1,<1; python_version>'3.13'",
    ],
)
def test_unverifiable_minimum_or_unbounded_contract_is_rejected(requirement):
    with pytest.raises(ValueError):
        compatibility.sdk_requirement({"dependencies": [requirement]})


def test_core_does_not_force_the_plugin_worker_to_use_its_host_sdk():
    requirements = compatibility.development_requirements(
        {
            "dependency-groups": {
                "dev": [{"include-group": "test"}, "toolframe-core==0.8.0"],
                "test": ["pytest>=8", "toolframe-sql-sqlite>=0.4,<1"],
            },
            "tool": {"toolframe-compatibility": {"test-dependencies": ["httpx"]}},
        }
    )
    assert requirements == ["pytest>=8", "toolframe-sql-sqlite>=0.4,<1", "httpx"]


def test_dependency_group_cycles_fail_before_installation():
    with pytest.raises(ValueError, match="cycle"):
        compatibility.development_requirements(
            {"dependency-groups": {"dev": [{"include-group": "dev"}]}}
        )
