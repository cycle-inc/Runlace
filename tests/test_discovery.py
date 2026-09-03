"""Discovery failure handling. Nothing here needs a working server."""

from __future__ import annotations

import asyncio

from mcp.types import Tool, ToolAnnotations

from runlace.config import Connector
from runlace.discovery import _looks_like_auth_failure, _to_discovered, discover_all, discover_one


def stdio(command: str = "definitely-not-a-real-command") -> Connector:
    return Connector(name="broken", attr="broken", transport="stdio", command=command)


def test_unlaunchable_server_becomes_an_error_result_not_an_exception() -> None:
    result = asyncio.run(discover_one(stdio(), timeout=10))
    assert result.status == "error"
    assert result.detail
    assert result.tools == []


def test_a_hanging_server_times_out() -> None:
    # `sleep` starts fine but never speaks MCP, so initialize() blocks.
    connector = Connector(
        name="slow", attr="slow", transport="stdio", command="sleep", args=["30"]
    )
    result = asyncio.run(discover_one(connector, timeout=1.0))
    assert result.status == "error"
    assert "timed out" in (result.detail or "")


def test_one_bad_server_does_not_stop_the_others() -> None:
    results = asyncio.run(discover_all([stdio("nope-a"), stdio("nope-b")], timeout=10))
    assert [r.status for r in results] == ["error", "error"]


def test_unreachable_remote_is_an_error() -> None:
    connector = Connector(
        name="remote", attr="remote", transport="http", url="http://127.0.0.1:9/mcp"
    )
    result = asyncio.run(discover_one(connector, timeout=10))
    assert result.status in ("error", "skipped")


def test_auth_failures_are_recognised() -> None:
    assert _looks_like_auth_failure("HTTPStatusError: 401 Unauthorized")
    assert _looks_like_auth_failure("server replied with www-authenticate")
    assert not _looks_like_auth_failure("ConnectionRefusedError: [Errno 61]")


def test_tool_conversion_keeps_the_wire_shape() -> None:
    tool = Tool(
        name="get-thing",
        description="Gets a thing.",
        input_schema={"type": "object", "properties": {"id": {"type": "string"}}},
        annotations=ToolAnnotations(read_only_hint=True),
    )
    discovered = _to_discovered(tool)
    assert discovered.name == "get-thing"  # verbatim, hyphen and all
    assert discovered.annotations == {"readOnlyHint": True}  # camelCase, as MCP sends it
    assert discovered.output_schema is None


def test_tool_without_annotations_converts_to_none() -> None:
    tool = Tool(name="x", input_schema={"type": "object"})
    assert _to_discovered(tool).annotations is None
