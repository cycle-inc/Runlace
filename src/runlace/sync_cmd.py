"""What ``runlace sync`` does: re-discover, then say what it cost you.

`init` imports a connector list and connects to it. `sync` connects to the list
already in ``config.json`` and, crucially, compares the answer to what was there
before. An MCP server is somebody else's software: parameters get renamed, tools
get retired, whole servers stop answering. D1 pins each workflow to the schema
hashes it compiled against, so none of that can silently change what a stored
workflow does -- but the workflow stops running, and the point of this command
is to find that out on purpose rather than at 3am.

The report has two halves, and the second is the one that matters: not "these
four tools changed", but "these two workflows will now be refused".
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import db
from .config import Connector, read_config
from .discovery import DEFAULT_TIMEOUT_SECONDS
from .init_cmd import ConnectorRow, InitReport, discover_and_persist
from .paths import RunlacePaths
from .workflows import get_workflow, list_workflows


@dataclass
class ToolChange:
    """One tool that is not what it was."""

    connector: str
    tool: str
    kind: str  # added | removed | changed


@dataclass
class BrokenWorkflow:
    """A stored workflow whose next run would be refused, and why."""

    name: str
    version: str
    reasons: list[str]


@dataclass
class SyncReport:
    home: str
    rows: list[ConnectorRow] = field(default_factory=list[ConnectorRow])
    warnings: list[str] = field(default_factory=list[str])
    changes: list[ToolChange] = field(default_factory=list[ToolChange])
    broken: list[BrokenWorkflow] = field(default_factory=list[BrokenWorkflow])
    workflows_checked: int = 0
    unreachable: list[str] = field(default_factory=list[str])

    @property
    def ok(self) -> bool:
        return not self.broken

    def counts(self) -> dict[str, int]:
        return {
            kind: sum(1 for c in self.changes if c.kind == kind)
            for kind in ("added", "removed", "changed")
        }


ADDED = "added"
REMOVED = "removed"
CHANGED = "changed"


def run_sync(
    paths: RunlacePaths, *, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> SyncReport:
    """Re-discover every configured connector and report what moved."""
    connectors: list[Connector] = read_config(paths.config)
    report = SyncReport(home=str(paths.home))

    before = _snapshot(paths)

    # `discover_and_persist` reports through an InitReport; sync's own report
    # carries more, so it borrows one and takes what it needs back out.
    written = InitReport(home=paths.home)
    results = discover_and_persist(paths, connectors, written, timeout=timeout)
    report.rows = written.rows
    report.warnings = written.warnings
    report.unreachable = [
        r.connector.name for r in results if r.status != "connected"
    ]

    after = _snapshot(paths)
    report.changes = _diff(before, after)
    report.broken, report.workflows_checked = _affected(paths)
    return report


def _snapshot(paths: RunlacePaths) -> dict[tuple[str, str], str]:
    """Every known tool's schema hash. Missing database means nothing is known."""
    if not paths.db.exists():
        return {}
    conn = db.connect(paths.db)
    try:
        return db.tool_schema_hashes(conn)
    finally:
        conn.close()


def _diff(
    before: dict[tuple[str, str], str], after: dict[tuple[str, str], str]
) -> list[ToolChange]:
    changes: list[ToolChange] = []
    for key in sorted(before.keys() | after.keys()):
        connector, tool = key
        if key not in after:
            changes.append(ToolChange(connector, tool, REMOVED))
        elif key not in before:
            changes.append(ToolChange(connector, tool, ADDED))
        elif before[key] != after[key]:
            changes.append(ToolChange(connector, tool, CHANGED))
    return changes


def _affected(paths: RunlacePaths) -> tuple[list[BrokenWorkflow], int]:
    """The stored workflows whose latest version would now be refused.

    Only the latest version of each: older ones are history, and D1 keeps them
    readable whatever happens to the servers.
    """
    conn = db.connect(paths.db)
    try:
        stored = list_workflows(conn)
        broken: list[BrokenWorkflow] = []
        for summary in stored:
            record = get_workflow(conn, str(summary["name"]))
            if record is None:
                continue
            drift = record.get("drift") or {}
            if drift.get("ok", True):
                continue
            reasons = [
                f"{t['connector']}.{t['tool']} changed its schema"
                for t in drift.get("changed", [])
            ] + [
                f"{t['connector']}.{t['tool']} no longer exists"
                for t in drift.get("missing", [])
            ]
            broken.append(
                BrokenWorkflow(
                    name=str(summary["name"]),
                    version=str(record.get("version")),
                    reasons=reasons,
                )
            )
        return broken, len(stored)
    finally:
        conn.close()


def format_changes(changes: list[ToolChange]) -> str:
    """The tool-level half of the report."""
    if not changes:
        return "No tool changed."
    marks = {ADDED: "+", REMOVED: "-", CHANGED: "~"}
    return "\n".join(
        f"  {marks[c.kind]} {c.connector}.{c.tool}" for c in changes
    )


def format_broken(broken: list[BrokenWorkflow]) -> str:
    """The half that decides whether anyone has work to do."""
    lines: list[str] = []
    for workflow in broken:
        lines.append(f"  {workflow.name} ({workflow.version})")
        lines.extend(f"      {reason}" for reason in workflow.reasons)
    return "\n".join(lines)
