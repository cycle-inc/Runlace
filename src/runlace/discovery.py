"""Connecting to MCP servers and listing their tools.

This is the only module that talks to the outside world. It connects, calls
``tools/list``, and hands back plain dictionaries in the exact shape the server
sent them -- tool names verbatim, schemas unaltered (D2).
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal

# The MCP SDK ships and uses httpx2; its own docs say to build the client with
# it when you need custom headers.
from httpx2 import AsyncClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Tool

from .config import Connector

Status = Literal["connected", "skipped", "error"]

DEFAULT_TIMEOUT_SECONDS = 30.0

# Substrings in a connection error that mean "this server wants OAuth", which
# v1 does not do (D8).
_AUTH_HINTS = ("401", "unauthorized", "invalid_token", "www-authenticate")


@dataclass
class DiscoveredTool:
    name: str
    description: str | None
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    annotations: dict[str, Any] | None


@dataclass
class DiscoveryResult:
    connector: Connector
    status: Status
    detail: str | None = None
    tools: list[DiscoveredTool] = field(default_factory=list[DiscoveredTool])


def _to_discovered(tool: Tool) -> DiscoveredTool:
    annotations = (
        tool.annotations.model_dump(by_alias=True, exclude_none=True)
        if tool.annotations is not None
        else None
    )
    return DiscoveredTool(
        name=tool.name,
        description=tool.description,
        input_schema=tool.input_schema or {},
        output_schema=tool.output_schema,
        annotations=annotations or None,
    )


@asynccontextmanager
async def _transport(connector: Connector) -> AsyncIterator[tuple[Any, Any]]:
    """Open the right client for the connector's transport."""
    if connector.transport == "stdio":
        assert connector.command is not None
        params = StdioServerParameters(
            command=connector.command,
            args=connector.args,
            # The SDK's default environment carries PATH and friends; without
            # it, launchers like npx cannot be found.
            env={**get_default_environment(), **connector.env},
        )
        async with stdio_client(params) as (read, write):
            yield read, write
        return

    assert connector.url is not None
    if connector.transport == "sse":
        async with sse_client(connector.url, headers=connector.headers or None) as (
            read,
            write,
        ):
            yield read, write
        return

    async with AsyncExitStack() as stack:
        # Without custom headers, let the SDK build a client with its own
        # recommended timeouts.
        http_client = (
            await stack.enter_async_context(AsyncClient(headers=connector.headers))
            if connector.headers
            else None
        )
        read, write = await stack.enter_async_context(
            streamable_http_client(connector.url, http_client=http_client)
        )
        yield read, write


async def _list_tools(connector: Connector) -> list[DiscoveredTool]:
    async with _transport(connector) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            return [_to_discovered(t) for t in result.tools]


async def discover_one(
    connector: Connector, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> DiscoveryResult:
    """Connect to one server. Never raises -- failures become a result."""
    try:
        async with asyncio.timeout(timeout):
            tools = await _list_tools(connector)
    except asyncio.TimeoutError:
        return DiscoveryResult(connector, "error", f"timed out after {timeout:.0f}s")
    except Exception as exc:  # noqa: BLE001 - one bad server must not stop init
        message = f"{type(exc).__name__}: {exc}".strip()
        if connector.transport != "stdio" and _looks_like_auth_failure(message):
            return DiscoveryResult(connector, "skipped", "oauth - see docs")
        return DiscoveryResult(connector, "error", _first_line(message))
    return DiscoveryResult(connector, "connected", None, tools)


async def discover_all(
    connectors: list[Connector], timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> list[DiscoveryResult]:
    """Connect to every server, one at a time so the output stays readable."""
    return [await discover_one(c, timeout) for c in connectors]


def _looks_like_auth_failure(message: str) -> bool:
    lowered = message.lower()
    return any(hint in lowered for hint in _AUTH_HINTS)


def _first_line(message: str, limit: int = 200) -> str:
    line = message.splitlines()[0] if message.splitlines() else message
    return line[:limit]
