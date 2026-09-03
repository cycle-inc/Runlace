from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from runlace import db


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return db.connect(tmp_path / "runlace.db")


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r["name"] for r in rows}


def test_bootstrap_creates_the_spec_tables(conn: sqlite3.Connection) -> None:
    assert {"workflows", "workflow_versions", "runs", "steps"} <= table_names(conn)


def test_bootstrap_creates_the_discovery_tables(conn: sqlite3.Connection) -> None:
    assert {"connectors", "tools"} <= table_names(conn)


def test_connect_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "runlace.db"
    db.connect(path).close()
    second = db.connect(path)
    assert second.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[
        "value"
    ] == str(db.SCHEMA_VERSION)


def add_connector(conn: sqlite3.Connection, name: str = "everything", tool_count: int = 1) -> None:
    db.replace_connector(
        conn,
        name=name,
        attr=name.replace("-", "_"),
        transport="stdio",
        config={"command": "npx"},
        status="connected",
        detail=None,
        tool_count=tool_count,
    )


def add_tool(conn: sqlite3.Connection, connector: str = "everything", name: str = "echo") -> None:
    db.insert_tool(
        conn,
        connector=connector,
        name=name,
        method=name,
        description="Echoes.",
        input_schema={"type": "object"},
        output_schema=None,
        annotations={"readOnlyHint": True},
        risk="read_only",
        schema_hash="deadbeef",
    )


def test_connector_and_tools_round_trip(conn: sqlite3.Connection) -> None:
    add_connector(conn)
    add_tool(conn)
    row = conn.execute("SELECT * FROM tools").fetchone()
    assert (row["connector"], row["name"], row["risk"]) == ("everything", "echo", "read_only")
    assert row["output_schema_json"] is None


def test_tool_names_are_stored_verbatim(conn: sqlite3.Connection) -> None:
    add_connector(conn)
    add_tool(conn, name="getWeather-v2")
    assert conn.execute("SELECT name FROM tools").fetchone()["name"] == "getWeather-v2"


def test_rediscovery_replaces_tools_rather_than_accumulating(conn: sqlite3.Connection) -> None:
    add_connector(conn)
    add_tool(conn, name="old")
    add_connector(conn)  # a second discovery pass
    add_tool(conn, name="new")
    assert [r["name"] for r in conn.execute("SELECT name FROM tools")] == ["new"]


def test_duplicate_tool_in_one_pass_is_rejected(conn: sqlite3.Connection) -> None:
    add_connector(conn)
    add_tool(conn)
    with pytest.raises(sqlite3.IntegrityError):
        add_tool(conn)


def test_prune_drops_connectors_missing_from_the_config(conn: sqlite3.Connection) -> None:
    add_connector(conn, "keep")
    add_tool(conn, connector="keep")
    add_connector(conn, "drop")
    add_tool(conn, connector="drop")
    db.prune_connectors(conn, ["keep"])
    assert [r["name"] for r in conn.execute("SELECT name FROM connectors")] == ["keep"]
    assert [r["connector"] for r in conn.execute("SELECT connector FROM tools")] == ["keep"]


def test_prune_with_nothing_to_keep_empties_both_tables(conn: sqlite3.Connection) -> None:
    add_connector(conn)
    add_tool(conn)
    db.prune_connectors(conn, [])
    assert conn.execute("SELECT count(*) AS n FROM tools").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM connectors").fetchone()["n"] == 0


def test_deleting_a_connector_cascades_to_its_tools(conn: sqlite3.Connection) -> None:
    add_connector(conn)
    add_tool(conn)
    conn.execute("DELETE FROM connectors WHERE name='everything'")
    assert conn.execute("SELECT count(*) AS n FROM tools").fetchone()["n"] == 0
