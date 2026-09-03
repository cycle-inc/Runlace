"""What ``runlace init`` actually does.

Import configs -> connect -> ``tools/list`` -> persist -> generate stubs. Kept
out of ``cli.py`` so tests can drive it without going through Typer.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from . import db, stubs
from .config import Connector, import_from_files, write_config
from .discovery import DEFAULT_TIMEOUT_SECONDS, DiscoveredTool, DiscoveryResult, discover_all
from .hashing import schema_hash
from .naming import python_identifier
from .paths import RunlacePaths
from .risk import classify


@dataclass
class ConnectorRow:
    """One line of the summary table."""

    name: str
    transport: str
    tools: int
    status: str


@dataclass
class InitReport:
    home: Path
    rows: list[ConnectorRow] = field(default_factory=list[ConnectorRow])
    warnings: list[str] = field(default_factory=list[str])
    stub_files: list[Path] = field(default_factory=list[Path])

    @property
    def tool_count(self) -> int:
        return sum(r.tools for r in self.rows)

    @property
    def connected(self) -> list[ConnectorRow]:
        return [r for r in self.rows if r.status == "connected"]


@dataclass
class _PlannedTool:
    """A discovered tool, classified and hashed, ready to be written."""

    tool: DiscoveredTool
    method: str
    risk: str
    schema_hash: str


def plan_connector(
    result: DiscoveryResult, warnings: list[str]
) -> tuple[stubs.ConnectorSpec, list[_PlannedTool]]:
    """Decide method names, risk and hashes. Pure: touches nothing."""
    connector = result.connector
    spec = stubs.ConnectorSpec(name=connector.name, attr=connector.attr)
    planned: list[_PlannedTool] = []

    used_methods: set[str] = set()
    for tool in sorted(result.tools, key=lambda t: t.name):
        method = python_identifier(tool.name)
        if method is None or method in used_methods:
            warnings.append(
                f"{connector.name}.{tool.name}: cannot be spelled as a unique Python "
                f"method name, tool skipped"
            )
            continue
        used_methods.add(method)

        risk = classify(tool.annotations)
        planned.append(
            _PlannedTool(
                tool=tool,
                method=method,
                risk=risk,
                schema_hash=schema_hash(tool.input_schema, tool.output_schema),
            )
        )
        spec.tools.append(
            stubs.ToolSpec(
                name=tool.name,
                method=method,
                description=tool.description,
                input_schema=tool.input_schema,
                output_schema=tool.output_schema,
                risk=risk,
            )
        )
    return spec, planned


def _write_connector(
    conn: db.Connection, result: DiscoveryResult, planned: list[_PlannedTool]
) -> None:
    """Write the connector row first, then its tools -- the tools reference it."""
    connector = result.connector
    db.replace_connector(
        conn,
        name=connector.name,
        attr=connector.attr,
        transport=connector.transport,
        config=connector.to_json(),
        status=result.status,
        detail=result.detail,
        tool_count=len(planned),
    )
    for item in planned:
        db.insert_tool(
            conn,
            connector=connector.name,
            name=item.tool.name,
            method=item.method,
            description=item.tool.description,
            input_schema=item.tool.input_schema,
            output_schema=item.tool.output_schema,
            annotations=item.tool.annotations,
            risk=item.risk,
            schema_hash=item.schema_hash,
        )


def run_init(
    paths: RunlacePaths,
    sources: list[Path],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> InitReport:
    paths.create()
    report = InitReport(home=paths.home)

    imported = import_from_files(sources)
    report.warnings.extend(imported.warnings)
    connectors: list[Connector] = imported.connectors
    write_config(paths.config, connectors)

    results = asyncio.run(discover_all(connectors, timeout=timeout))

    conn = db.connect(paths.db)
    try:
        db.prune_connectors(conn, [c.name for c in connectors])
        specs: list[stubs.ConnectorSpec] = []
        for result in results:
            spec, planned = plan_connector(result, report.warnings)
            _write_connector(conn, result, planned)
            # Only servers we could reach get a stub; a stub for an unreachable
            # server would let a workflow typecheck against tools we never saw.
            if result.status == "connected":
                specs.append(spec)
            report.rows.append(
                ConnectorRow(
                    name=result.connector.name,
                    transport=result.connector.transport,
                    tools=len(planned),
                    status=result.status
                    if result.detail is None
                    else f"{result.status} ({result.detail})",
                )
            )
        conn.commit()
    finally:
        conn.close()

    generated = stubs.generate(paths, specs)
    report.stub_files = generated.files
    report.warnings.extend(generated.warnings)
    return report


def format_table(rows: list[ConnectorRow]) -> str:
    """The summary table `runlace init` prints."""
    headers = ("SERVER", "TRANSPORT", "TOOLS", "STATUS")
    cells = [headers] + [(r.name, r.transport, str(r.tools), r.status) for r in rows]
    widths = [max(len(row[i]) for row in cells) for i in range(len(headers))]
    lines = [
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in cells
    ]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)
