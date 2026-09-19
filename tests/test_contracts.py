import pytest
from pydantic import ValidationError

from toolframe_sdk import (
    Migration,
    PluginDefinition,
    Resource,
    StoreSchema,
    digest,
    validate,
)


def test_definition_round_trip_and_owned_identity():
    definition = PluginDefinition(
        id="example",
        factory="example:Plugin",
        resources=(
            Resource(
                id="main",
                kind="dashboard.page",
                data={"entrypoint": "example.ui:render"},
            ),
        ),
    )
    assert (
        PluginDefinition.model_validate_json(definition.model_dump_json()) == definition
    )
    with pytest.raises(ValidationError, match="Duplicate"):
        PluginDefinition(
            id="example", factory="example:Plugin", resources=definition.resources * 2
        )


@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", "a/../../secret", ""])
def test_document_cannot_escape_package(path):
    with pytest.raises(ValidationError):
        Resource(
            id="usage", kind="llms.document", data={"package": "example", "path": path}
        )


def test_migration_history_and_content_identity():
    first = Migration(revision="one", sql="CREATE TABLE entries(id INTEGER);")
    second = Migration(
        revision="two", parent="one", sql="ALTER TABLE entries ADD COLUMN label TEXT;"
    )
    schema = StoreSchema(revision="two", migrations=(first, second))
    assert len(first.checksum) == 64
    assert digest(schema.model_dump()) != digest(
        schema.model_copy(update={"revision": "other"}).model_dump()
    )
    with pytest.raises(ValidationError, match="linear"):
        StoreSchema(revision="two", migrations=(second, first))


def test_non_finite_data_is_not_json_even_with_open_schema():
    with pytest.raises(ValueError):
        validate({}, {"value": float("nan")})
