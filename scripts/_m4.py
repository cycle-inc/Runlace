"""Shared by the steps of `m4_acceptance.sh`, which each run as their own process."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent

# Written against nothing but what `get_skill` returned: the connector index
# gives `ctx.everything.echo` and `ctx.everything.get_sum`, and the stub gives
# their parameters and the fact that both are read-only.
CODE = """\
from runlace_types import Ctx


def run(ctx: Ctx) -> dict[str, object]:
    greeting = ctx.everything.echo(message=ctx.inputs["message"])
    total = ctx.everything.get_sum(a=ctx.inputs["a"], b=ctx.inputs["b"])
    return {"greeting": str(greeting), "total": str(total)}
"""

INPUTS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "message": {"type": "string"},
        "a": {"type": "number"},
        "b": {"type": "number"},
    },
    "required": ["message", "a", "b"],
}

ARGUMENTS: dict[str, Any] = {"message": "good morning", "a": 20, "b": 22}


def call(server: MCPServer, tool: str, /, **arguments: Any) -> dict[str, Any]:
    """One MCP tool call, the way a client makes it."""
    result = asyncio.run(server.call_tool(tool, arguments))
    assert isinstance(result, CallToolResult), result
    assert not result.is_error, result.content
    if result.structured_content is not None:
        return dict(result.structured_content)
    block = result.content[0]
    assert isinstance(block, TextContent), block
    return dict(json.loads(block.text))
