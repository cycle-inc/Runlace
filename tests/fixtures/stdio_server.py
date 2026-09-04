"""A minimal stdio MCP server with one tool that acts.

The worker executes runs in a subprocess and every tool call travels back over
a pipe to a real MCP session -- so a test that wants to prove an approved
side-effect run really runs needs a real server to send the email to. This is
that server, and it writes what it was asked to send to a file so the test can
see it happened.

Run as: python stdio_server.py <outbox>
"""

from __future__ import annotations

import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer

server = MCPServer("gmail")
OUTBOX = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("outbox.txt")


@server.tool()
def send_email(to: str, subject: str, body: str) -> str:
    """Send an email."""
    with OUTBOX.open("a", encoding="utf-8") as handle:
        handle.write(f"{to}\t{subject}\t{body}\n")
    return f"sent to {to}"


if __name__ == "__main__":
    server.run(transport="stdio")
