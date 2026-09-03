"""A minimal streamable-HTTP MCP server, for exercising remote discovery.

Run as: python http_server.py <port>
"""

from __future__ import annotations

import sys
import time

import uvicorn
from mcp.server.mcpserver import MCPServer

server = MCPServer("probe")


@server.tool()
def ping(message: str) -> str:
    """Replies with the message."""
    return message


@server.tool()
def slow(seconds: float) -> str:
    """Answers after a delay, to exercise the transport's read timeout."""
    time.sleep(seconds)
    return f"waited {seconds}"


if __name__ == "__main__":
    uvicorn.run(
        server.streamable_http_app(),
        host="127.0.0.1",
        port=int(sys.argv[1]),
        log_level="error",
    )
