from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Iterator

import pytest

from runlace.db import Connection, connect, insert_tool, replace_connector
from runlace.hashing import schema_hash
from runlace.naming import python_identifier
from runlace.paths import RunlacePaths
from runlace.risk import classify
from runlace.stubs import ConnectorSpec, ToolSpec, generate

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RunlacePaths:
    """An isolated Runlace home, so tests never touch the real ~/.runlace."""
    home = tmp_path / "runlace-home"
    monkeypatch.setenv("RUNLACE_HOME", str(home))
    p = RunlacePaths(home)
    p.create()
    return p


FAKE_CONNECTORS: list[tuple[str, str, list[dict[str, Any]]]] = [
    (
        "pennylane",
        "pennylane",
        [
            {
                "name": "list_transactions",
                "description": "List transactions in a period.",
                "input_schema": {
                    "type": "object",
                    "properties": {"from": {"type": "string"}, "to": {"type": "string"}},
                    "required": ["from", "to"],
                },
                "output_schema": {
                    "type": "object",
                    "properties": {
                        "transactions": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string"},
                                    "amount": {"type": "number"},
                                },
                                "required": ["id", "amount"],
                            },
                        }
                    },
                    "required": ["transactions"],
                },
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "get_balance",
                "description": "Current balance.",
                "input_schema": {"type": "object", "properties": {}},
                "output_schema": None,
                "annotations": {"readOnlyHint": True},
            },
        ],
    ),
    (
        "gmail",
        "gmail",
        [
            {
                "name": "send_email",
                "description": "Send an email.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string"},
                        "subject": {"type": "string"},
                        "body": {"type": "string"},
                    },
                    "required": ["to", "subject", "body"],
                },
                "output_schema": None,
                "annotations": None,  # unannotated -> side_effect (D5)
            }
        ],
    ),
]


@pytest.fixture
def home(paths: RunlacePaths) -> Iterator[tuple[RunlacePaths, Connection]]:
    """A Runlace home populated as if `init` had reached two MCP servers.

    Two connectors, a reserved-word parameter, a tool with an output schema and
    one without, and a read-only/side-effect mix -- enough for the compiler to
    have something real to check against without needing a live server.
    """
    conn = connect(paths.db)
    specs: list[ConnectorSpec] = []
    for name, attr, tools in FAKE_CONNECTORS:
        replace_connector(
            conn,
            name=name,
            attr=attr,
            transport="stdio",
            config={"command": "true"},
            status="connected",
            detail=None,
            tool_count=len(tools),
        )
        tool_specs: list[ToolSpec] = []
        for tool in tools:
            method = python_identifier(str(tool["name"])) or str(tool["name"])
            risk = classify(tool["annotations"])
            insert_tool(
                conn,
                connector=name,
                name=str(tool["name"]),
                method=method,
                description=tool["description"],
                input_schema=tool["input_schema"],
                output_schema=tool["output_schema"],
                annotations=tool["annotations"],
                risk=risk,
                schema_hash=schema_hash(tool["input_schema"], tool["output_schema"]),
            )
            tool_specs.append(
                ToolSpec(
                    name=str(tool["name"]),
                    method=method,
                    description=tool["description"],
                    input_schema=tool["input_schema"],
                    output_schema=tool["output_schema"],
                    risk=risk,
                )
            )
        specs.append(ConnectorSpec(name=name, attr=attr, tools=tool_specs))
    conn.commit()
    generate(paths, specs)
    try:
        yield paths, conn
    finally:
        conn.close()


@pytest.fixture(scope="session")
def npx() -> str:
    executable = shutil.which("npx")
    if executable is None:
        pytest.skip("npx is not on PATH")
    return executable
