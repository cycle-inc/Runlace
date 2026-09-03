"""Live MCP sessions for the duration of one run.

:mod:`runlace.discovery` connects once to ask what a server can do. This is the
other kind of connection: opened when a run starts, kept for as long as the
workflow is executing, and closed when it ends. Every session is opened up front
rather than on first use, so a server that is down fails the run before any
workflow code has executed and before any side effect has happened.
"""

from __future__ import annotations

import json
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, AsyncIterator

from mcp import ClientSession
from mcp.types import CallToolResult, TextContent

from .config import Connector
from .discovery import open_transport
from .runner import DEFAULT_TOOL_TIMEOUT_SECONDS, ToolFailed


class ConnectorSessions:
    """The open sessions of one run, keyed by verbatim connector name."""

    def __init__(self, sessions: dict[str, ClientSession], *, timeout: float) -> None:
        self._sessions = sessions
        self._timeout = timeout

    async def call(
        self, connector: str, tool: str, arguments: dict[str, Any]
    ) -> Any:
        """Perform one MCP tool call. Raises :class:`ToolFailed` if it does not work."""
        session = self._sessions.get(connector)
        if session is None:
            raise ToolFailed(f"no open session for connector `{connector}`")
        result = await session.call_tool(
            tool, arguments, read_timeout_seconds=self._timeout
        )
        if not isinstance(result, CallToolResult):
            # An elicitation request. Workflows run with no model and no human
            # in the loop, so there is nobody to answer it.
            raise ToolFailed(
                f"`{tool}` asked for more input mid-call, which a workflow cannot answer"
            )
        if result.is_error:
            raise ToolFailed(_error_text(result))
        return tool_payload(result)


@asynccontextmanager
async def open_sessions(
    connectors: list[Connector], *, timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> AsyncIterator[ConnectorSessions]:
    """Connect to each connector, yield the pool, and close everything after."""
    async with AsyncExitStack() as stack:
        sessions: dict[str, ClientSession] = {}
        for connector in connectors:
            read, write = await stack.enter_async_context(open_transport(connector))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            sessions[connector.name] = session
        yield ConnectorSessions(sessions, timeout=timeout)


def tool_payload(result: CallToolResult) -> Any:
    """What the workflow receives back from a tool call.

    Structured content when the tool declares an output schema -- that is what
    the generated stub promised the workflow. Otherwise the text blocks, with a
    lone one parsed if it turns out to hold JSON.
    """
    if result.structured_content is not None:
        return result.structured_content
    texts = [block.text for block in result.content if isinstance(block, TextContent)]
    if len(texts) == len(result.content) and texts:
        return _parsed(texts[0]) if len(texts) == 1 else texts
    return [json.loads(block.model_dump_json()) for block in result.content]


def _parsed(text: str) -> Any:
    """``text`` as a dict or a list if that is what it holds, else unchanged.

    Servers that declare no output schema still overwhelmingly answer with JSON
    serialised into a text block -- every one of GitHub's 47 tools does. Handing
    the workflow the string would make it call ``json.loads`` itself, which D3
    allows but which is busywork built on a guess.

    Only a dict or a list. A tool whose answer is genuinely a string must keep
    it: ``"42"`` is an id, not the number 42, and ``"null"`` is a word. Parsing
    those would be a silent corruption rather than a convenience.
    """
    try:
        value = json.loads(text)
    except ValueError:
        return text
    return value if isinstance(value, (dict, list)) else text


def _error_text(result: CallToolResult) -> str:
    texts = [block.text for block in result.content if isinstance(block, TextContent)]
    return " ".join(texts).strip() or "the tool reported an error"
