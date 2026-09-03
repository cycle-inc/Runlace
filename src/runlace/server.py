"""The Runlace MCP server.

What an agent talks to. Every tool returns structured JSON and every docstring
is written for a model to read, because the docstrings *are* the interface.

Each call opens its own SQLite connection: MCP hosts call tools concurrently and
sqlite3 connections are not shared across threads.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from mcp.server.mcpserver import MCPServer

from .db import Connection, connect
from .paths import RunlacePaths, paths as default_paths
from .runs import run_workflow as _run_workflow
from .skill import build_skill
from .workflows import create_workflow as _create_workflow
from .workflows import get_workflow as _get_workflow
from .workflows import list_workflows as _list_workflows

SERVER_NAME = "runlace"

INSTRUCTIONS = """\
Runlace stores deterministic, replayable workflows over this machine's MCP
servers. Call get_skill first: it returns the calling convention and the exact
connectors and tools available here. Then create_workflow with the code, and
run_workflow to execute it.
"""


def build_server(paths: RunlacePaths | None = None) -> MCPServer:
    """Create the MCP server. ``paths`` is injectable so tests can isolate it."""
    home = paths or default_paths()
    server = MCPServer(SERVER_NAME, instructions=INSTRUCTIONS)

    @contextmanager
    def session() -> Iterator[Connection]:
        conn = connect(home.db)
        try:
            yield conn
        finally:
            conn.close()

    @server.tool()
    def get_skill() -> dict[str, Any]:
        """Learn how to write a Runlace workflow, and what this machine can do.

        Call this first. Returns the skill document (calling convention, file
        contract, forbidden patterns), an index of every connected MCP server
        with its tools and their risk, and the generated type stubs those
        workflows are checked against.
        """
        with session() as conn:
            return build_skill(conn, home)

    @server.tool()
    def create_workflow(
        name: str,
        description: str,
        code: str,
        inputs_schema: dict[str, Any],
        outputs_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Compile Python into a stored, immutable workflow version.

        The code must define `def run(ctx)` and call tools as
        `ctx.<connector>.<tool>(**kwargs)`; see get_skill. It is linted,
        typechecked with pyright in strict mode against the generated stubs, and
        its tool calls are extracted from the source -- you never declare which
        tools it uses.

        `name` is lowercase letters, digits, hyphens and underscores.
        `inputs_schema` is a JSON Schema for what the workflow reads from
        `ctx.inputs`; anything a user might vary between runs belongs there
        rather than inline. Declare `outputs_schema` only if you also annotate
        `run` as `-> Output`.

        On failure returns {ok: false, stage, errors: [{line, message, hint}]};
        fix what it reports and call again. On success returns {ok: true,
        workflow_id, version, tools_used, warnings}. Creating under an existing
        name adds a version -- it never overwrites the previous one.
        """
        with session() as conn:
            result = _create_workflow(
                conn,
                home,
                name=name,
                description=description,
                code=code,
                inputs_schema=inputs_schema,
                outputs_schema=outputs_schema,
            )
        return result.to_json()

    @server.tool()
    def list_workflows() -> dict[str, Any]:
        """List every stored workflow: name, description, latest version, last run."""
        with session() as conn:
            return {"workflows": _list_workflows(conn)}

    @server.tool()
    def get_workflow(
        workflow: str, version: str | None = None
    ) -> dict[str, Any]:
        """Read one workflow in full, by workflow_id or by name.

        Returns its code, schemas, the tools it uses with their risk, the list of
        versions, and its schema-drift status -- whether the tools it was
        compiled against still have the contracts it was pinned to. Pass
        `version` to read an older version instead of the latest.
        """
        with session() as conn:
            record = _get_workflow(conn, workflow, version=version)
        if record is None:
            return {
                "ok": False,
                "error": f"no workflow called `{workflow}`",
                "hint": "Call list_workflows to see what exists.",
            }
        return record

    @server.tool()
    async def run_workflow(
        workflow_id: str,
        inputs: dict[str, Any] | None = None,
        confirm: bool = False,
        version: str | None = None,
    ) -> dict[str, Any]:
        """Execute a stored workflow. No model is involved: it just runs.

        `workflow_id` may also be the workflow's name. `inputs` must match the
        workflow's inputs_schema; call get_workflow if you are unsure what it
        declares.

        Call this WITHOUT `confirm` first. If the workflow uses any tool that
        acts on the world, it is refused with {code: "needs-confirmation",
        side_effects: [...]}. Show the human exactly which tools those are, and
        call again with confirm=True only after they agree in the conversation.

        Returns {ok, run_id, status, output, steps}. Every attempt is journaled,
        refusals included, so `run_id` is always worth keeping. On failure the
        result carries `code`, `error` and `hint`.
        """
        with session() as conn:
            return await _run_workflow(
                conn,
                home,
                workflow=workflow_id,
                inputs=inputs,
                confirm=confirm,
                version=version,
            )

    return server


def serve(paths: RunlacePaths | None = None, *, port: int | None = None) -> None:
    """Run the server on stdio, or over streamable HTTP when a port is given."""
    server = build_server(paths)
    if port is None:
        server.run(transport="stdio")
    else:
        server.run(transport="streamable-http", port=port)
