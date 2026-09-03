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
from runlace.server import build_server

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


def test_the_server_exposes_m2s_tools(home: tuple[RunlacePaths, Connection]) -> None:
    paths, _ = home
    server = build_server(paths)
    tools = tools_of(server)
    assert set(tools) == {
        "get_skill",
        "create_workflow",
        "list_workflows",
        "get_workflow",
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


def test_run_workflow_is_not_exposed_yet(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """M3's tool. Advertising it before it works would be worse than absent."""
    paths, _ = home
    assert "run_workflow" not in tools_of(build_server(paths))
