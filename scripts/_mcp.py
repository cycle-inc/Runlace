"""Calling the Runlace MCP server in-process, the way a client would.

The acceptance scripts and the demo all go through this rather than reaching
into `runlace.workflows` directly: what they are checking is the surface an
agent actually sees.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent


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
