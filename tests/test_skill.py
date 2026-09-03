"""`get_skill` and `get_tools`, at the level of the functions underneath them.

The split between the two is the point: the index is what an agent reads to
choose, the signatures are what it reads to call, and only the second one grows
with the size of the connector.
"""

from __future__ import annotations

from typing import Any

import pytest

from runlace.db import Connection, connect, insert_tool, replace_connector
from runlace.hashing import schema_hash
from runlace.paths import RunlacePaths
from runlace.skill import build_skill, tool_types

SUM_INPUT = {
    "type": "object",
    "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
    "required": ["a", "b"],
}


@pytest.fixture
def conn(paths: RunlacePaths) -> Connection:
    """One connector whose MCP tool name is not a legal Python identifier."""
    connection = connect(paths.db)
    replace_connector(
        connection,
        name="everything",
        attr="everything",
        transport="stdio",
        config={"command": "true"},
        status="connected",
        detail=None,
        tool_count=2,
    )
    for name, method, schema in (
        ("get-sum", "get_sum", SUM_INPUT),
        ("echo", "echo", {"type": "object", "properties": {"message": {"type": "string"}}}),
    ):
        insert_tool(
            connection,
            connector="everything",
            name=name,
            method=method,
            description=f"{name} does something.",
            input_schema=schema,
            output_schema=None,
            annotations={"readOnlyHint": True},
            risk="read_only",
            schema_hash=schema_hash(schema, None),
        )
    connection.commit()
    return connection


def test_the_index_carries_names_and_risk_but_no_signatures(conn: Connection) -> None:
    """What makes get_skill cheap: it grows one line per tool, not one type."""
    skill = build_skill(conn)

    tools = skill["connectors"][0]["tools"]
    assert {t["tool"] for t in tools} == {"get-sum", "echo"}
    assert set(tools[0]) == {"tool", "call", "description", "risk"}
    assert "stubs" not in skill


def test_asking_for_one_tool_returns_only_that_one(conn: Connection) -> None:
    result = tool_types(conn, "everything", ["echo"])

    assert result["ok"] is True
    assert "def echo" in result["types"]
    assert "def get_sum" not in result["types"]


def test_either_spelling_of_a_tool_name_works(conn: Connection) -> None:
    """The index shows `get-sum` and `ctx.everything.get_sum(...)`. Take both."""
    by_mcp_name = tool_types(conn, "everything", ["get-sum"])
    by_method = tool_types(conn, "everything", ["get_sum"])

    assert by_mcp_name["types"] == by_method["types"]
    assert "def get_sum" in by_method["types"]
    assert by_method["unknown"] == []


def test_the_signature_says_which_arguments_are_required(conn: Connection) -> None:
    """The whole reason this tool exists, rather than the one-line description."""
    types: str = tool_types(conn, "everything", ["get-sum"])["types"]

    assert "def get_sum(self, *, a: float, b: float)" in types
    assert "(risk: read_only)" in types


def test_omitting_the_tool_list_gives_the_whole_connector(conn: Connection) -> None:
    result = tool_types(conn, "everything")

    assert "def echo" in result["types"]
    assert "def get_sum" in result["types"]


def test_asking_only_for_tools_that_do_not_exist_is_an_error(
    conn: Connection,
) -> None:
    """Returning an empty stub would read as "this connector has nothing"."""
    result = tool_types(conn, "everything", ["nope"])

    assert result["ok"] is False
    assert result["code"] == "unknown-tool"


def test_an_unknown_connector_does_not_pretend_to_be_empty(conn: Connection) -> None:
    result: dict[str, Any] = tool_types(conn, "stripe")

    assert result["ok"] is False
    assert result["code"] == "unknown-connector"
