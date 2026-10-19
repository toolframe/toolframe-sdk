"""The public SQL client preserves routing and transaction ownership."""

from unittest.mock import AsyncMock

import pytest

from toolframe_sdk import RuntimeContext, SqlClient


@pytest.mark.parametrize("alias", [None, "reporting"])
async def test_sql_transaction_keeps_alias_and_transaction_identity(alias):
    peer = AsyncMock()
    peer.tracing = None
    peer.request.side_effect = [
        {"transaction": "tx-1"},
        {"rows": [{"id": 1}]},
        {"committed": True},
    ]
    context = RuntimeContext(peer, plugin_id="notes", generation="g1")
    client = context.sql() if alias is None else context.sql(alias)
    assert isinstance(client, SqlClient)

    async with client.transaction(mutation_id="write-1") as transaction:
        result = await transaction.execute_returning(
            "INSERT INTO notes(label) VALUES (?) RETURNING id",
            ["hello"],
            mutation_id="statement-1",
        )

    assert result == {"rows": [{"id": 1}]}
    requests = [call.args for call in peer.request.call_args_list]
    assert [method for method, _ in requests] == ["service.call"] * 3
    assert [params["alias"] for _, params in requests] == [alias or "sql"] * 3
    assert [params["method"] for _, params in requests] == [
        "begin",
        "execute_returning",
        "commit",
    ]
    assert requests[1][1]["params"]["transaction"] == "tx-1"
    assert requests[1][1]["params"]["mutation_id"] == "statement-1"
    assert requests[2][1]["params"] == {"transaction": "tx-1"}


async def test_sql_transaction_rolls_back_when_business_write_fails():
    peer = AsyncMock()
    peer.tracing = None
    peer.request.side_effect = [{"transaction": "tx-2"}, {}]
    context = RuntimeContext(peer, plugin_id="notes", generation="g1")

    with pytest.raises(ValueError, match="write failed"):
        async with context.sql().transaction(mutation_id="write-2"):
            raise ValueError("write failed")

    request = peer.request.call_args.args[1]
    assert request["alias"] == "sql"
    assert request["method"] == "rollback"
    assert request["params"] == {"transaction": "tx-2"}
