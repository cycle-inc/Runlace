"""`runlace init` orchestration, driven with synthetic discovery results."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from runlace.config import Connector
from runlace.discovery import DiscoveredTool, DiscoveryResult
from runlace.init_cmd import ConnectorRow, format_table, plan_connector, run_init
from runlace.model import Model, write_model
from runlace.paths import RunlacePaths
from runlace.policy import Policy

from .conftest import FIXTURES


def tool(name: str, annotations: dict[str, Any] | None = None) -> DiscoveredTool:
    return DiscoveredTool(
        name=name,
        description=f"Does {name}.",
        input_schema={"type": "object", "properties": {"a": {"type": "string"}}},
        output_schema=None,
        annotations=annotations,
    )


def result(*tools: DiscoveredTool, status: str = "connected") -> DiscoveryResult:
    connector = Connector(name="srv", attr="srv", transport="stdio", command="x")
    return DiscoveryResult(connector, status, None, list(tools))  # type: ignore[arg-type]


def test_plan_sorts_tools_and_maps_method_names() -> None:
    spec, planned = plan_connector(result(tool("z-tool"), tool("a-tool")), [])
    assert [p.tool.name for p in planned] == ["a-tool", "z-tool"]
    assert [p.method for p in planned] == ["a_tool", "z_tool"]
    assert [t.method for t in spec.tools] == ["a_tool", "z_tool"]


def test_plan_applies_risk_classification() -> None:
    _, planned = plan_connector(
        result(tool("read", {"readOnlyHint": True}), tool("write")), []
    )
    assert {p.tool.name: p.risk for p in planned} == {
        "read": "read_only",
        "write": "side_effect",
    }


def test_plan_lets_policy_override_the_classification() -> None:
    """The override has to land here, before the risk is written and pinned."""
    policy = Policy(risk={"srv": {"write": "read_only", "read": "side_effect"}})
    _, planned = plan_connector(
        result(tool("read", {"readOnlyHint": True}), tool("write")), [], policy
    )
    assert {p.tool.name: p.risk for p in planned} == {
        "read": "side_effect",
        "write": "read_only",
    }


def test_plan_hashes_every_tool() -> None:
    _, planned = plan_connector(result(tool("a")), [])
    assert len(planned[0].schema_hash) == 64


def test_tool_description_is_not_part_of_the_hash() -> None:
    """Chosen behaviour: rewording a description must not invalidate workflows."""
    first = plan_connector(result(tool("a")), [])[1][0]
    renamed = tool("a")
    renamed.description = "A completely different description."
    second = plan_connector(result(renamed), [])[1][0]
    assert first.schema_hash == second.schema_hash


def test_tools_colliding_on_one_method_name_are_skipped_with_a_warning() -> None:
    warnings: list[str] = []
    _, planned = plan_connector(result(tool("do-thing"), tool("do_thing")), warnings)
    assert len(planned) == 1
    assert any("do_thing" in w or "do-thing" in w for w in warnings)


def test_run_init_writes_config_db_and_stubs(paths: RunlacePaths) -> None:
    report = run_init(paths, [FIXTURES / "claude_config.json"], timeout=1.0)

    assert paths.config.exists()
    assert paths.db.exists()
    assert (paths.types / "ctx.pyi").exists()
    # Every server in that fixture is unreachable, so none of them connect.
    assert report.connected == []


def test_unreachable_servers_are_recorded_but_get_no_stub(paths: RunlacePaths) -> None:
    run_init(paths, [FIXTURES / "claude_config.json"], timeout=1.0)

    conn = sqlite3.connect(paths.db)
    conn.row_factory = sqlite3.Row
    try:
        statuses = {r["name"]: r["status"] for r in conn.execute("SELECT name, status FROM connectors")}
    finally:
        conn.close()

    assert set(statuses) == {"global-stdio", "remote-api", "project-sse"}
    assert all(s != "connected" for s in statuses.values())
    # A stub would let a workflow typecheck against tools we never saw.
    assert list(paths.connectors.glob("*.pyi")) == [paths.connectors / "__init__.pyi"]
    assert "inputs: Inputs" in (paths.types / "ctx.pyi").read_text()


def test_rerunning_init_with_fewer_servers_prunes_the_old_ones(paths: RunlacePaths) -> None:
    run_init(paths, [FIXTURES / "claude_config.json"], timeout=1.0)
    smaller = paths.home / "smaller.json"
    smaller.write_text(json.dumps({"mcpServers": {"only": {"command": "x"}}}), encoding="utf-8")

    run_init(paths, [smaller], timeout=1.0)

    conn = sqlite3.connect(paths.db)
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM connectors")]
    finally:
        conn.close()
    assert names == ["only"]


def test_init_reports_a_policy_override_that_names_nothing(paths: RunlacePaths) -> None:
    """The typo has to be visible at init; nothing later will mention it."""
    paths.create()
    paths.policy.write_text("risk:\n  ghub:\n    search: read_only\n", encoding="utf-8")

    report = run_init(paths, [FIXTURES / "claude_config.json"], timeout=1.0)

    assert any("risk.ghub.search matches no known tool" in w for w in report.warnings)


def test_an_override_for_the_configured_model_is_not_a_typo(
    paths: RunlacePaths,
) -> None:
    """`ctx.ai` is overridable per model, and no server hosts a tool by that name."""
    paths.create()
    write_model(paths.model, Model(base_url="https://api.openai.com/v1", model="gpt-4o-mini"))
    paths.policy.write_text(
        "risk:\n  ai:\n    gpt-4o-mini: read_only\n", encoding="utf-8"
    )

    report = run_init(paths, [FIXTURES / "claude_config.json"], timeout=1.0)

    assert not any("risk.ai" in w for w in report.warnings)


def test_an_override_for_a_model_this_machine_does_not_use_is_reported(
    paths: RunlacePaths,
) -> None:
    paths.create()
    write_model(paths.model, Model(base_url="https://api.openai.com/v1", model="gpt-4o-mini"))
    paths.policy.write_text("risk:\n  ai:\n    gpt-5: read_only\n", encoding="utf-8")

    report = run_init(paths, [FIXTURES / "claude_config.json"], timeout=1.0)

    assert any("risk.ai.gpt-5 matches no known tool" in w for w in report.warnings)


def test_format_table_aligns_and_includes_status() -> None:
    table = format_table(
        [
            ConnectorRow("everything", "stdio", 13, "connected"),
            ConnectorRow("acme", "http", 0, "skipped (oauth - see docs)"),
        ]
    )
    lines = table.splitlines()
    assert lines[0].startswith("SERVER")
    assert "skipped (oauth - see docs)" in table
    assert len(lines) == 4  # header, rule, two rows
