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
from .workflows import edit_workflow as _edit_workflow
from .workflows import get_workflow as _get_workflow
from .workflows import list_workflows as _list_workflows

SERVER_NAME = "runlace"

INSTRUCTIONS = """\
Runlace stores deterministic, replayable workflows over this machine's MCP
servers. Call get_skill first: it returns the calling convention and the exact
connectors and tools available here. Then create_workflow with the code,
dry_run_workflow to check it really works, edit_workflow to fix what it turns
up, and run_workflow to execute it for real.
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
        rather than inline. It is required and is never null: a workflow that
        reads nothing declares {"type": "object", "properties": {}}. Declare
        `outputs_schema` only if you also annotate `run` as `-> Output`.

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
    def edit_workflow(
        name: str,
        old_string: str,
        new_string: str,
        description: str | None = None,
        inputs_schema: dict[str, Any] | None = None,
        outputs_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Change one exact string in a workflow's code, and recompile it.

        Use this instead of create_workflow whenever you are fixing existing
        code: sending the whole file back to change one line is where most of
        the mistakes come from. Call get_workflow first and copy `old_string`
        out of the code you get back, exactly as it appears, indentation
        included.

        `old_string` must match once and only once -- include the surrounding
        lines until it does. Runlace will not guess which occurrence you meant.

        The schemas and the description carry over from the version you edited;
        pass one only to change it. The result is a NEW immutable version, same
        as create_workflow: the previous one stays readable and runnable.

        On failure returns {ok: false, stage, errors: [{message, hint, code}]}
        with `code` one of unknown-workflow, missing-code, no-change, no-match,
        not-unique -- or the usual lint/typecheck errors if the edited code no
        longer compiles.
        """
        with session() as conn:
            result = _edit_workflow(
                conn,
                home,
                name=name,
                old_string=old_string,
                new_string=new_string,
                description=description,
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

    @server.tool()
    async def dry_run_workflow(
        workflow_id: str,
        inputs: dict[str, Any] | None = None,
        version: str | None = None,
    ) -> dict[str, Any]:
        """Run a workflow for real, but let nothing act on the world.

        This is how you check a workflow before you tell a human it works.
        Compiling proves the calls have the right shape; only running proves the
        code survives what the tools actually return.

        Every read hits the live server and gets the real answer. Every tool
        that acts -- anything Runlace calls a side effect -- is answered from its
        own declared output shape instead of being called, so nothing is sent,
        created or deleted. There is no confirm gate here, because there is
        nothing to confirm.

        Same result as run_workflow, plus {dry_run: true, simulated: [...]}.
        `simulated` is the list of calls that were stood in for: a branch that
        depends on what one of them really returns is the one thing this cannot
        check, so read it before trusting the output. The run is journaled like
        any other, and marked so it never counts as "this workflow last ran".
        """
        with session() as conn:
            return await _run_workflow(
                conn,
                home,
                workflow=workflow_id,
                inputs=inputs,
                dry_run=True,
                version=version,
            )

    return server


def serve(
    paths: RunlacePaths | None = None,
    *,
    port: int | None = None,
    host: str = "127.0.0.1",
) -> None:
    """Run the server on stdio, or over streamable HTTP when a port is given.

    ``host`` stays on loopback unless you say otherwise. A container cannot
    reach loopback on its host, so a Dockerised MCP client (Open WebUI,
    LibreChat) needs ``0.0.0.0`` -- which also turns off the DNS-rebinding
    protection the MCP SDK enables for localhost, because the client will send
    a ``Host`` header this process has never heard of.
    """
    server = build_server(paths)
    if port is None:
        server.run(transport="stdio")
    else:
        server.run(transport="streamable-http", host=host, port=port)
