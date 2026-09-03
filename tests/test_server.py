"""The MCP server: the surface an agent actually sees.

Exercised through `MCPServer.list_tools` / `call_tool` rather than by calling
the underlying functions, so the tool schemas, the descriptions and the
structured results are all part of what these tests check.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult

from runlace.db import Connection
from runlace.paths import RunlacePaths
from runlace.server import build_server, serve

INPUTS = {
    "type": "object",
    "properties": {"to": {"type": "string"}},
    "required": ["to"],
}

BALANCE = (
    "from runlace_types import Ctx\n\n\n"
    "def run(ctx: Ctx) -> dict[str, object]:\n"
    "    ctx.pennylane.get_balance()\n"
    '    return {"to": ctx.inputs["to"]}\n'
)

SENDS_EMAIL = (
    "from runlace_types import Ctx\n\n\n"
    "def run(ctx: Ctx) -> dict[str, object]:\n"
    '    ctx.gmail.send_email(to=ctx.inputs["to"], subject="s", body="b")\n'
    "    return {}\n"
)


def call(server: MCPServer, tool: str, /, **arguments: Any) -> dict[str, Any]:
    result = asyncio.run(server.call_tool(tool, arguments))
    # The other branch is an elicitation request; none of these tools ask for one.
    assert isinstance(result, CallToolResult), result
    assert not result.is_error, result.content
    if result.structured_content is not None:
        return dict(result.structured_content)
    return dict(json.loads(result.content[0].text))  # type: ignore[union-attr]


def tools_of(server: MCPServer) -> dict[str, Any]:
    return {t.name: t for t in asyncio.run(server.list_tools())}


def test_the_server_exposes_the_seven_tools(
    home: tuple[RunlacePaths, Connection]
) -> None:
    paths, _ = home
    server = build_server(paths)
    tools = tools_of(server)
    assert set(tools) == {
        "get_skill",
        "create_workflow",
        "edit_workflow",
        "list_workflows",
        "get_workflow",
        "run_workflow",
        "dry_run_workflow",
    }
    # Descriptions are the interface for a model; none may be empty.
    assert all(t.description for t in tools.values())


def test_create_workflow_declares_its_arguments(
    home: tuple[RunlacePaths, Connection]
) -> None:
    paths, _ = home
    server = build_server(paths)
    schema = tools_of(server)["create_workflow"].input_schema
    assert set(schema["required"]) == {"name", "description", "code", "inputs_schema"}
    assert "outputs_schema" in schema["properties"]


def test_the_no_inputs_spelling_is_written_down_where_an_agent_will_read_it(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """`inputs_schema` is required and non-null, so "no inputs" needs a spelling.

    A model that sends `null` gets a Pydantic error out of the tool layer and no
    compiler verdict at all -- there is no line, no hint, nothing to fix. The
    empty object is the answer, and it has to appear in both places an agent
    reads before it calls: the tool's own description and SKILL.md.
    """
    paths, _ = home
    server = build_server(paths)
    empty = '{"type": "object", "properties": {}}'
    assert empty in (tools_of(server)["create_workflow"].description or "")
    assert empty in call(server, "get_skill")["skill"]


def test_get_skill_lists_the_connectors_and_their_stubs(
    home: tuple[RunlacePaths, Connection]
) -> None:
    paths, _ = home
    skill = call(build_server(paths), "get_skill")

    assert "ctx.<connector>.<tool>(**kwargs)" in skill["skill"]
    connectors = {c["connector"]: c for c in skill["connectors"]}
    assert set(connectors) == {"pennylane", "gmail"}

    tools = {t["tool"]: t for t in connectors["pennylane"]["tools"]}
    assert tools["list_transactions"]["risk"] == "read_only"
    assert tools["list_transactions"]["call"] == "ctx.pennylane.list_transactions(...)"
    assert connectors["gmail"]["tools"][0]["risk"] == "side_effect"

    assert "runlace_types/connectors/pennylane.pyi" in skill["stubs"]
    assert "def list_transactions" in skill["stubs"]["runlace_types/connectors/pennylane.pyi"]


def test_create_then_get_round_trips(home: tuple[RunlacePaths, Connection]) -> None:
    paths, _ = home
    server = build_server(paths)

    created = call(
        server,
        "create_workflow",
        name="report",
        description="A report.",
        code=BALANCE,
        inputs_schema=INPUTS,
    )
    assert created["ok"] is True
    assert [t["tool"] for t in created["tools_used"]] == ["get_balance"]

    listed = call(server, "list_workflows")
    assert [w["name"] for w in listed["workflows"]] == ["report"]

    record = call(server, "get_workflow", workflow="report")
    assert record["code"] == BALANCE
    assert record["version"] == created["version"]
    assert record["drift"]["ok"] is True


def test_a_rejected_workflow_comes_back_as_actionable_errors(
    home: tuple[RunlacePaths, Connection]
) -> None:
    paths, _ = home
    server = build_server(paths)
    result = call(
        server,
        "create_workflow",
        name="report",
        description="d",
        code=(
            "from runlace_types import Ctx\n\n\n"
            "def run(ctx: Ctx) -> dict[str, object]:\n"
            "    ctx.pennylane.nope()\n"
            "    return {}\n"
        ),
        inputs_schema=INPUTS,
    )
    assert result["ok"] is False
    assert result["stage"] == "typecheck"
    assert result["errors"][0]["line"] == 5
    assert result["errors"][0]["hint"]


def test_side_effect_warnings_reach_the_agent(
    home: tuple[RunlacePaths, Connection]
) -> None:
    paths, _ = home
    result = call(
        build_server(paths),
        "create_workflow",
        name="notify",
        description="d",
        code=SENDS_EMAIL,
        inputs_schema=INPUTS,
    )
    assert result["ok"] is True
    assert any("confirm=True" in w for w in result["warnings"])


def test_getting_an_unknown_workflow_says_what_to_do(
    home: tuple[RunlacePaths, Connection]
) -> None:
    paths, _ = home
    result = call(build_server(paths), "get_workflow", workflow="nope")
    assert result["ok"] is False
    assert "list_workflows" in result["hint"]


def test_run_workflow_declares_its_arguments(
    home: tuple[RunlacePaths, Connection]
) -> None:
    paths, _ = home
    schema = tools_of(build_server(paths))["run_workflow"].input_schema
    assert schema["required"] == ["workflow_id"]
    assert set(schema["properties"]) == {"workflow_id", "inputs", "confirm", "version"}


def test_run_workflow_refuses_a_side_effect_without_confirm(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """The gate that matters most, seen exactly as the agent sees it (D6)."""
    paths, _ = home
    server = build_server(paths)
    call(
        server,
        "create_workflow",
        name="notify",
        description="d",
        code=SENDS_EMAIL,
        inputs_schema=INPUTS,
    )

    result = call(
        server, "run_workflow", workflow_id="notify", inputs={"to": "a@b.c"}
    )
    assert result["ok"] is False
    assert result["code"] == "needs-confirmation"
    assert result["side_effects"] == [{"connector": "gmail", "tool": "send_email"}]
    # Refused is still journaled: the agent gets a run_id to show the human.
    assert result["run_id"].startswith("run_")


def test_run_workflow_reports_a_server_it_cannot_reach(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """The fixture's connectors are not real. Failing to connect is a run failure."""
    paths, _ = home
    server = build_server(paths)
    call(
        server,
        "create_workflow",
        name="report",
        description="d",
        code=BALANCE,
        inputs_schema=INPUTS,
    )

    result = call(server, "run_workflow", workflow_id="report", inputs={"to": "a@b.c"})
    assert result["ok"] is False
    assert result["code"] == "connector-unreachable"
    assert "runlace init" in result["hint"]


def test_running_an_unknown_workflow_says_what_to_do(
    home: tuple[RunlacePaths, Connection]
) -> None:
    paths, _ = home
    result = call(build_server(paths), "run_workflow", workflow_id="nope")
    assert result["ok"] is False
    assert result["code"] == "unknown-workflow"
    assert "list_workflows" in result["hint"]


def test_a_workflow_with_no_inputs_compiles_with_the_empty_schema(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """The spelling SKILL.md advertises has to actually be accepted."""
    created = call(
        build_server(home[0]),
        "create_workflow",
        name="no-inputs",
        description="Reads the balance and nothing else.",
        code=(
            "from runlace_types import Ctx\n\n\n"
            "def run(ctx: Ctx) -> dict[str, object]:\n"
            '    return {"balance": ctx.pennylane.get_balance()}\n'
        ),
        inputs_schema={"type": "object", "properties": {}},
    )
    assert created["ok"] is True


def test_a_new_version_is_told_it_has_never_run(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """The nudge towards the dry run has to reach the agent that just compiled.

    Nobody calls an optional verification step they were never told about; this
    is the same reasoning as the confirm gate, one notch softer.
    """
    created = call(
        build_server(home[0]),
        "create_workflow",
        name="report",
        description="d",
        code=BALANCE,
        inputs_schema=INPUTS,
    )
    assert any("dry_run_workflow" in w for w in created["warnings"])


def test_edit_workflow_declares_its_arguments(
    home: tuple[RunlacePaths, Connection]
) -> None:
    paths, _ = home
    schema = tools_of(build_server(paths))["edit_workflow"].input_schema
    assert set(schema["required"]) == {"name", "old_string", "new_string"}
    assert set(schema["properties"]) == {
        "name",
        "old_string",
        "new_string",
        "description",
        "inputs_schema",
        "outputs_schema",
    }


def test_editing_a_workflow_stores_a_new_version_and_keeps_the_old_one(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """The whole point of the tool, seen the way an agent sees it (D1 intact)."""
    paths, _ = home
    server = build_server(paths)
    created = call(
        server,
        "create_workflow",
        name="report",
        description="A report.",
        code=BALANCE,
        inputs_schema=INPUTS,
    )

    edited = call(
        server,
        "edit_workflow",
        name="report",
        old_string='return {"to": ctx.inputs["to"]}',
        new_string='return {"who": ctx.inputs["to"]}',
    )
    assert edited["ok"] is True
    assert edited["version"] != created["version"]

    latest = call(server, "get_workflow", workflow="report")
    assert '"who"' in latest["code"]
    # Schemas carried over untouched, and the version we edited is still there.
    assert latest["inputs_schema"] == INPUTS
    old = call(server, "get_workflow", workflow="report", version=created["version"])
    assert old["code"] == BALANCE


def test_an_edit_that_matches_nothing_says_how_to_get_the_text_right(
    home: tuple[RunlacePaths, Connection]
) -> None:
    paths, _ = home
    server = build_server(paths)
    call(
        server,
        "create_workflow",
        name="report",
        description="d",
        code=BALANCE,
        inputs_schema=INPUTS,
    )

    result = call(
        server, "edit_workflow", name="report", old_string="nope", new_string="x"
    )
    assert result["ok"] is False
    assert result["stage"] == "edit"
    assert result["errors"][0]["code"] == "no-match"
    assert "get_workflow" in result["errors"][0]["hint"]


def test_dry_run_workflow_declares_its_arguments(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """No `confirm`: there is nothing to confirm when nothing can act."""
    paths, _ = home
    schema = tools_of(build_server(paths))["dry_run_workflow"].input_schema
    assert schema["required"] == ["workflow_id"]
    assert set(schema["properties"]) == {"workflow_id", "inputs", "version"}


def test_a_dry_run_is_not_stopped_by_the_confirm_gate(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """Same workflow that run_workflow refuses, dry-run without confirm.

    The fixture's gmail server does not exist, and that is the point: the tool
    that would act is stood in for, so the dry run never needs to reach it.
    """
    paths, _ = home
    server = build_server(paths)
    call(
        server,
        "create_workflow",
        name="notify",
        description="d",
        code=SENDS_EMAIL,
        inputs_schema=INPUTS,
    )

    result = call(
        server, "dry_run_workflow", workflow_id="notify", inputs={"to": "a@b.c"}
    )
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["simulated"] == [{"connector": "gmail", "tool": "send_email"}]


# -- the transport ---


def test_serve_without_a_port_uses_stdio(
    home: tuple[RunlacePaths, Connection], monkeypatch: Any
) -> None:
    """The default an MCP host launching `runlace serve` gets."""
    paths, _ = home
    seen: dict[str, Any] = {}
    monkeypatch.setattr(MCPServer, "run", lambda self, **kw: seen.update(kw))

    serve(paths)

    assert seen == {"transport": "stdio"}


def test_serve_with_a_port_stays_on_loopback_unless_told_otherwise(
    home: tuple[RunlacePaths, Connection], monkeypatch: Any
) -> None:
    """Binding wider than loopback has to be asked for, never inferred."""
    paths, _ = home
    seen: dict[str, Any] = {}
    monkeypatch.setattr(MCPServer, "run", lambda self, **kw: seen.update(kw))

    serve(paths, port=8000)

    assert seen == {"transport": "streamable-http", "host": "127.0.0.1", "port": 8000}


def test_serve_binds_the_host_it_is_given(
    home: tuple[RunlacePaths, Connection], monkeypatch: Any
) -> None:
    """What a Dockerised MCP client needs: a container cannot reach loopback."""
    paths, _ = home
    seen: dict[str, Any] = {}
    monkeypatch.setattr(MCPServer, "run", lambda self, **kw: seen.update(kw))

    serve(paths, port=8000, host="0.0.0.0")

    assert seen["host"] == "0.0.0.0"
