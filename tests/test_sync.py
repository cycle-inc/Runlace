"""`runlace sync`: re-discover, then say which stored workflows that broke.

The tool-level diff is the easy half. The half worth testing is the second one:
D1 pins every workflow to the schema hashes it compiled against, so a server
renaming a parameter has to surface as "this workflow will now be refused" and
not merely as "this tool changed".

The fixture below stands in for the network. It starts out returning exactly
what the `home` fixture already wrote to the database, so a sync that changes
nothing really does report nothing; each test then edits that list to play the
part of a server that moved overnight.
"""

from __future__ import annotations

from typing import Any, Iterator

import pytest

from runlace.config import Connector
from runlace.db import Connection
from runlace.discovery import DiscoveredTool, DiscoveryResult
from runlace.paths import RunlacePaths
from runlace.sync_cmd import ADDED, CHANGED, REMOVED, SyncReport, run_sync
from runlace.workflows import create_workflow

CODE = (
    "from runlace_types import Ctx\n\n\n"
    "def run(ctx: Ctx) -> dict[str, object]:\n"
    "    ctx.pennylane.get_balance()\n"
    '    return {"ok": True}\n'
)

TRANSACTIONS_INPUT: dict[str, Any] = {
    "type": "object",
    "properties": {"from": {"type": "string"}, "to": {"type": "string"}},
    "required": ["from", "to"],
}
TRANSACTIONS_OUTPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "transactions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "amount": {"type": "number"}},
                "required": ["id", "amount"],
            },
        }
    },
    "required": ["transactions"],
}


def read_only(
    name: str,
    input_schema: dict[str, Any],
    output_schema: dict[str, Any] | None = None,
) -> DiscoveredTool:
    return DiscoveredTool(
        name=name,
        description=f"Does {name}.",
        input_schema=input_schema,
        output_schema=output_schema,
        annotations={"readOnlyHint": True},
    )


def pennylane_as_discovered() -> list[DiscoveredTool]:
    """The same two tools the `home` fixture stored, hash for hash."""
    return [
        read_only("get_balance", {"type": "object", "properties": {}}),
        read_only("list_transactions", TRANSACTIONS_INPUT, TRANSACTIONS_OUTPUT),
    ]


GMAIL_AS_DISCOVERED = [
    DiscoveredTool(
        name="send_email",
        description="Send an email.",
        input_schema={
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["to", "subject", "body"],
        },
        output_schema=None,
        annotations=None,
    )
]


@pytest.fixture
def synced(
    home: tuple[RunlacePaths, Connection], monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[RunlacePaths, Connection, list[DiscoveredTool]]]:
    """`home`, plus a discovery whose answer the test controls.

    The yielded list *is* what pennylane will report next time: append to it,
    replace an entry, or remove one, then call `run_sync`.
    """
    paths, conn = home
    tools = pennylane_as_discovered()

    async def fake_discover(
        connectors: list[Connector], *, timeout: float = 0.0
    ) -> list[DiscoveryResult]:
        answers = {"pennylane": tools, "gmail": GMAIL_AS_DISCOVERED}
        return [
            DiscoveryResult(c, "connected", None, list(answers[c.name]))
            for c in connectors
        ]

    monkeypatch.setattr("runlace.init_cmd.discover_all", fake_discover)
    yield paths, conn, tools


def store_balance_workflow(paths: RunlacePaths, conn: Connection) -> None:
    """A workflow that calls `pennylane.get_balance` and nothing else."""
    created = create_workflow(
        conn,
        paths,
        name="balance",
        description="Read the balance.",
        code=CODE,
        inputs_schema=None,
    )
    assert created.ok, [str(e) for e in created.errors]


def kinds(report: SyncReport) -> set[tuple[str, str]]:
    return {(c.kind, f"{c.connector}.{c.tool}") for c in report.changes}


def test_a_sync_that_finds_the_same_servers_reports_nothing(
    synced: tuple[RunlacePaths, Connection, list[DiscoveredTool]]
) -> None:
    paths, conn, _ = synced
    store_balance_workflow(paths, conn)

    report = run_sync(paths)

    assert report.changes == []
    assert report.counts() == {"added": 0, "removed": 0, "changed": 0}
    assert report.broken == []
    assert report.workflows_checked == 1
    assert report.ok


def test_a_removed_tool_breaks_the_workflow_that_used_it(
    synced: tuple[RunlacePaths, Connection, list[DiscoveredTool]]
) -> None:
    paths, conn, tools = synced
    store_balance_workflow(paths, conn)

    tools[:] = [t for t in tools if t.name != "get_balance"]
    report = run_sync(paths)

    assert kinds(report) == {(REMOVED, "pennylane.get_balance")}
    assert [w.name for w in report.broken] == ["balance"]
    assert report.broken[0].reasons == ["pennylane.get_balance no longer exists"]
    assert not report.ok


def test_a_changed_schema_breaks_the_workflow_pinned_to_it(
    synced: tuple[RunlacePaths, Connection, list[DiscoveredTool]]
) -> None:
    """The tool is still there and still callable -- and that is the point.

    Nothing about this failure is visible from the workflow's own code; only the
    pinned hash catches it.
    """
    paths, conn, tools = synced
    store_balance_workflow(paths, conn)

    tools[0] = read_only(
        "get_balance", {"type": "object", "properties": {"currency": {"type": "string"}}}
    )
    report = run_sync(paths)

    assert kinds(report) == {(CHANGED, "pennylane.get_balance")}
    assert report.broken[0].reasons == ["pennylane.get_balance changed its schema"]
    assert report.broken[0].version


def test_a_new_tool_breaks_nothing(
    synced: tuple[RunlacePaths, Connection, list[DiscoveredTool]]
) -> None:
    """Additive is the common case, and it must not read as alarming."""
    paths, conn, tools = synced
    store_balance_workflow(paths, conn)

    tools.append(read_only("list_invoices", {"type": "object", "properties": {}}))
    report = run_sync(paths)

    assert kinds(report) == {(ADDED, "pennylane.list_invoices")}
    assert report.broken == []
    assert report.ok


def test_a_workflow_that_uses_nothing_that_moved_is_left_alone(
    synced: tuple[RunlacePaths, Connection, list[DiscoveredTool]]
) -> None:
    paths, conn, tools = synced
    store_balance_workflow(paths, conn)

    tools[1] = read_only(
        "list_transactions",
        {"type": "object", "properties": {"page": {"type": "integer"}}},
        TRANSACTIONS_OUTPUT,
    )
    report = run_sync(paths)

    assert kinds(report) == {(CHANGED, "pennylane.list_transactions")}
    assert report.broken == []
    assert report.workflows_checked == 1


def test_the_stubs_are_regenerated_so_the_next_workflow_sees_the_new_tool(
    synced: tuple[RunlacePaths, Connection, list[DiscoveredTool]]
) -> None:
    """`sync` is not read-only: discovery is persisted, exactly as `init` does."""
    paths, _, tools = synced
    tools.append(read_only("list_invoices", {"type": "object", "properties": {}}))

    run_sync(paths)

    assert "def list_invoices" in (paths.types / "connectors" / "pennylane.pyi").read_text()


def test_a_server_that_stopped_answering_is_called_out(
    home: tuple[RunlacePaths, Connection], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every tool of a server "disappearing" at once usually means it is down.

    Recreating workflows on that evidence would be exactly the wrong move, so the
    unreachable connector is reported apart from the tool diff.
    """
    paths, conn = home
    store_balance_workflow(paths, conn)

    async def unreachable(
        connectors: list[Connector], *, timeout: float = 0.0
    ) -> list[DiscoveryResult]:
        return [DiscoveryResult(c, "error", "connection refused", []) for c in connectors]

    monkeypatch.setattr("runlace.init_cmd.discover_all", unreachable)
    report = run_sync(paths)

    assert report.unreachable == ["pennylane", "gmail"]
    assert [w.name for w in report.broken] == ["balance"]
