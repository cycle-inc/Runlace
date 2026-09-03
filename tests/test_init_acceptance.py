"""M1's acceptance criterion, as an executable test.

    runlace init --from fixtures/mcp.json
      -> stubs that pyright accepts
      -> a populated DB

Everything here runs against the real ``@modelcontextprotocol/server-everything``
over stdio, so it needs npx and a network on first run.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from runlace.config import import_from_files
from runlace.discovery import discover_one
from runlace.init_cmd import ConnectorRow, format_table, run_init
from runlace.paths import RunlacePaths
from runlace.typecheck import check_paths, check_stubs

from .conftest import FIXTURES

pytestmark = pytest.mark.needs_npx

TIMEOUT = 120.0


@pytest.fixture(scope="module")
def everything_tools() -> list[str]:
    """Tool names the demo server actually advertises, fetched once."""
    connector = import_from_files([FIXTURES / "mcp.json"]).connectors[0]
    result = asyncio.run(discover_one(connector, timeout=TIMEOUT))
    assert result.status == "connected", result.detail
    return [t.name for t in result.tools]


@pytest.fixture(scope="module")
def initialised(tmp_path_factory: pytest.TempPathFactory) -> RunlacePaths:
    """Run `runlace init --from fixtures/mcp.json` once for the whole module."""
    paths = RunlacePaths(tmp_path_factory.mktemp("home") / ".runlace")
    report = run_init(paths, [FIXTURES / "mcp.json"], timeout=TIMEOUT)
    assert [r.status for r in report.rows] == ["connected"], report.rows
    return paths


# --- the acceptance criterion, both halves --------------------------------


def test_generated_stubs_pass_pyright(initialised: RunlacePaths, npx: str) -> None:
    result = check_stubs(initialised.types)
    assert result.ok, result.report()
    assert result.files_analyzed >= 4


def test_database_is_populated(initialised: RunlacePaths, npx: str) -> None:
    conn = sqlite3.connect(initialised.db)
    conn.row_factory = sqlite3.Row
    try:
        connectors = conn.execute("SELECT * FROM connectors").fetchall()
        tools = conn.execute("SELECT * FROM tools").fetchall()
    finally:
        conn.close()

    assert len(connectors) == 1
    assert connectors[0]["name"] == "everything"
    assert connectors[0]["status"] == "connected"
    assert connectors[0]["tool_count"] == len(tools)
    assert len(tools) > 0


# --- the pieces that make it true -----------------------------------------


def test_every_tool_has_a_schema_hash_and_a_risk(initialised: RunlacePaths, npx: str) -> None:
    conn = sqlite3.connect(initialised.db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM tools").fetchall()
    finally:
        conn.close()
    for row in rows:
        assert len(row["schema_hash"]) == 64, row["name"]
        assert row["risk"] in ("read_only", "side_effect"), row["name"]
        json.loads(row["input_schema_json"])


def test_layout_matches_the_spec(initialised: RunlacePaths, npx: str) -> None:
    assert initialised.db.exists()
    assert initialised.config.exists()
    assert (initialised.types / "__init__.pyi").exists()
    assert (initialised.types / "ctx.pyi").exists()
    assert (initialised.connectors / "everything.pyi").exists()
    assert initialised.workflows.is_dir()


def test_config_records_the_imported_server(initialised: RunlacePaths, npx: str) -> None:
    doc = json.loads(initialised.config.read_text())
    assert doc["connectors"]["everything"]["transport"] == "stdio"
    assert doc["connectors"]["everything"]["command"] == "npx"


def test_ctx_exposes_the_connector(initialised: RunlacePaths, npx: str) -> None:
    ctx = (initialised.types / "ctx.pyi").read_text()
    assert "everything: Everything" in ctx


def test_stub_covers_every_discovered_tool(
    initialised: RunlacePaths, everything_tools: list[str], npx: str
) -> None:
    conn = sqlite3.connect(initialised.db)
    conn.row_factory = sqlite3.Row
    try:
        stored = {r["name"]: r["method"] for r in conn.execute("SELECT name, method FROM tools")}
    finally:
        conn.close()

    assert set(stored) == set(everything_tools)
    source = (initialised.connectors / "everything.pyi").read_text()
    for method in stored.values():
        assert f"def {method}(" in source, method


def test_a_workflow_written_against_the_stubs_typechecks(
    initialised: RunlacePaths, npx: str
) -> None:
    """The point of the stubs: real workflow code compiles against them."""
    workflow = initialised.home / "probe.py"
    workflow.write_text(
        "from runlace_types import Ctx\n"
        "\n"
        "def run(ctx: Ctx) -> dict[str, object]:\n"
        '    result = ctx.everything.echo(message="hi")\n'
        '    return {"echo": result}\n',
        encoding="utf-8",
    )
    checked = check_paths(initialised.home, [workflow], types_dir=initialised.types)
    assert checked.ok, checked.report()


def test_unknown_tool_fails_to_typecheck(initialised: RunlacePaths, npx: str) -> None:
    """The same machinery must reject a tool the server never advertised."""
    workflow = initialised.home / "bad_probe.py"
    workflow.write_text(
        "from runlace_types import Ctx\n"
        "\n"
        "def run(ctx: Ctx) -> dict[str, object]:\n"
        "    return {\"x\": ctx.everything.no_such_tool()}\n",
        encoding="utf-8",
    )
    checked = check_paths(initialised.home, [workflow], types_dir=initialised.types)
    assert not checked.ok
    assert any("no_such_tool" in e.message for e in checked.errors)


def test_summary_table_lists_the_server(initialised: RunlacePaths, npx: str) -> None:
    table = format_table([ConnectorRow("everything", "stdio", 8, "connected")])
    assert "SERVER" in table
    assert "everything" in table
