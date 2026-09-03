"""Remote (streamable-HTTP) discovery against a real server.

D8 puts remote servers with static header auth in scope, so the HTTP path needs
the same proof the stdio path gets.
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from runlace.config import Connector
from runlace.discovery import discover_one
from runlace.sessions import open_sessions

SERVER = Path(__file__).parent / "fixtures" / "http_server.py"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def http_server() -> Iterator[str]:
    port = free_port()
    process = subprocess.Popen(
        [sys.executable, str(SERVER), str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail("the test MCP server exited during startup")
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.1)
        else:
            pytest.fail("the test MCP server never started listening")
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        process.terminate()
        process.wait(timeout=10)


def remote(url: str, headers: dict[str, str] | None = None) -> Connector:
    return Connector(
        name="probe",
        attr="probe",
        transport="http",
        url=url,
        headers=headers or {},
    )


def test_discovers_tools_over_streamable_http(http_server: str) -> None:
    result = asyncio.run(discover_one(remote(http_server), timeout=30))
    assert result.status == "connected", result.detail
    assert [t.name for t in result.tools] == ["ping", "slow"]


def test_static_header_auth_is_sent(http_server: str) -> None:
    # D8's supported remote auth: a static header on every request.
    result = asyncio.run(
        discover_one(remote(http_server, {"Authorization": "Bearer test"}), timeout=30)
    )
    assert result.status == "connected", result.detail
    assert [t.name for t in result.tools] == ["ping", "slow"]


def test_a_connector_with_headers_keeps_the_sdks_read_timeout(http_server: str) -> None:
    """The regression this exists to prevent.

    Passing our own client to `streamable_http_client` opts out of the one it
    would have built, and a bare `httpx2.AsyncClient` reads for 5 seconds where
    the SDK's reads for 300. Since headers are the only reason we build a client
    at all, that made every authenticated remote server -- D8's whole case --
    the one kind that timed out on a slow tool.
    """
    connector = remote(http_server, {"Authorization": "Bearer test"})

    async def call_slow() -> object:
        async with open_sessions([connector], timeout=60) as sessions:
            return await sessions.call("probe", "slow", {"seconds": 6.5})

    assert "waited 6.5" in str(asyncio.run(call_slow()))


def test_wrong_path_is_reported_as_an_error(http_server: str) -> None:
    result = asyncio.run(discover_one(remote(http_server + "-nope"), timeout=30))
    assert result.status in ("error", "skipped")
    assert result.tools == []
