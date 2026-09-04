"""The Runlace MCP server.

What an agent talks to. Every tool returns structured JSON and every docstring
is written for a model to read, because the docstrings *are* the interface.

Each call opens its own SQLite connection: MCP hosts call tools concurrently and
sqlite3 connections are not shared across threads.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from typing import Any, AsyncIterator, Iterator

from mcp.server.mcpserver import MCPServer

from .connect_cmd import add_connector as _add_connector
from .db import Connection, connect
from .journal import get_run as _get_run
from .journal import get_step as _get_step
from .paths import RunlacePaths, paths as default_paths
from .runs import run_workflow as _run_workflow
from .skill import build_skill, tool_types
from .worker import Worker
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
up, and run_workflow to execute it for real. If what the human wants is not
among the connectors get_skill lists, add_connector connects a new server.
"""


def build_server(paths: RunlacePaths | None = None, *, drain: bool = False) -> MCPServer:
    """Create the MCP server. ``paths`` is injectable so tests can isolate it.

    ``drain`` also runs the queue worker for as long as the server is up. It is
    off by default because it is only true of a daemon: over stdio this process
    dies when the MCP host disconnects, and a run left going here would be
    killed mid-flight. See :mod:`runlace.worker`.
    """
    home = paths or default_paths()

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        if not drain:
            yield
            return
        stop = asyncio.Event()
        worker = asyncio.create_task(Worker(home).drain(stop))
        try:
            yield
        finally:
            stop.set()
            await worker

    server = MCPServer(SERVER_NAME, instructions=INSTRUCTIONS, lifespan=lifespan)

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
        contract, forbidden patterns) and an index of every connected MCP
        server: each tool's name, one line of description, and its risk.

        The index is what you need to pick your tools. It deliberately does not
        carry their signatures -- one connector can be fifty tools, and you are
        about to use three. Call get_tools for those three.
        """
        with session() as conn:
            return build_skill(conn)

    @server.tool()
    def get_tools(connector: str, tools: list[str] | None = None) -> dict[str, Any]:
        """Get the exact signatures of the tools you are about to call.

        The second half of get_skill, on demand. Returns `types`: the generated
        `.pyi` for those tools -- keyword arguments with their Python types,
        which are required, the return type, the full description and the risk.
        It is a slice of what pyright checks your code against, so a call
        written against it compiles.

        `tools` takes either spelling the index shows, the MCP name
        (`get-sum`) or the method (`get_sum`). Omit it to get the whole
        connector, which is worth it for a small one and expensive for a large
        one.

        Names you asked for that do not exist come back in `unknown` rather
        than failing the call.
        """
        with session() as conn:
            return tool_types(conn, connector, tools)

    # Deliberately sync: discovery calls asyncio.run, which cannot happen inside
    # a running loop. The SDK runs sync tools on a worker thread, so this is the
    # spelling that works -- an `async def` here would raise at the first call.
    @server.tool()
    def add_connector(
        name: str,
        url: str,
        headers: dict[str, str] | None = None,
        transport: str = "http",
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Connect a remote MCP server so workflows can use its tools.

        Use this when the human wants to reach something Runlace does not have
        yet -- get_skill lists what it does have. `name` becomes `ctx.<name>` in
        workflow code. `url` is the server's MCP endpoint, in full.

        Call this WITHOUT `confirm` first. It writes nothing and returns
        {code: "needs-confirmation"} with what it would add; show the human the
        URL and call again with confirm=True only after they agree. A connector
        widens what every future workflow on this machine can reach, so this is
        their decision, not yours.

        Credentials are never stored here. Put a `${VAR}` reference in `headers`
        -- {"Authorization": "Bearer ${GITHUB_TOKEN}"} -- and the value is read
        from the environment when the connection opens. A literal token is
        refused: config.json is a file on disk. Never ask the human to paste a
        token into the conversation; ask them to export it and restart
        `runlace serve`, which reads the environment once at startup.

        Only HTTP servers. A local one launched by a command is added from a
        shell with `runlace add <name> --command ...`, because that is a
        decision to run a program on the human's machine.

        On success returns {ok: true, attr, tools} -- then call get_skill again,
        the stubs have been regenerated. On failure, `code` is one of
        needs-confirmation, literal-secret, bad-name, bad-transport,
        name-collision, connector-unreachable.
        """
        return _add_connector(
            home,
            name=name,
            url=url,
            headers=headers,
            transport=transport,
            confirm=confirm,
        )

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
        wait: float | None = None,
    ) -> dict[str, Any]:
        """Execute a stored workflow. No model is involved: it just runs.

        `workflow_id` may also be the workflow's name. `inputs` must match the
        workflow's inputs_schema; call get_workflow if you are unsure what it
        declares.

        Call this WITHOUT `confirm` first. If the workflow uses any tool that
        acts on the world, it is refused with {code: "needs-confirmation",
        side_effects: [...]}. Show the human exactly which tools those are, and
        call again with confirm=True only after they agree in the conversation.

        By default this waits for the run to finish. `wait` is how many seconds
        to stay with it instead: pass 0 to get a run_id back immediately, or a
        number for a workflow you expect to be slow. When the time runs out you
        get {ok: false, code: "not-finished", status} -- nothing has gone wrong,
        the run is still going, and get_run with the same run_id is how you
        find out how it ended.

        Returns {ok, run_id, status, output, steps}. `steps` says which tools
        were called and how they went, without their arguments or their
        results -- a step that read a thousand rows would fill your context
        with the data the workflow was supposed to reduce. Use get_step when
        you need to see one. Every attempt is journaled, refusals included, so
        `run_id` is always worth keeping. On failure the result carries `code`,
        `error` and `hint`.
        """
        with session() as conn:
            return await _run_workflow(
                conn,
                home,
                workflow=workflow_id,
                inputs=inputs,
                confirm=confirm,
                version=version,
                wait=wait,
            )

    @server.tool()
    def get_run(run_id: str) -> dict[str, Any]:
        """Check how a run is going, or how it went.

        The other half of `wait`: a run you did not stay for is read back here.
        Returns the same shape run_workflow does -- {ok, status, output, steps}
        -- plus `finished`, which is false while it is still queued or running.

        A run can also be waiting on a human, and then `status` is
        awaiting_approval. There is nothing you can do about that from here: it
        is the person in front of the application who answers, not you, and
        this tool has no way to say yes on their behalf.
        """
        with session() as conn:
            return _get_run(conn, run_id)

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

    @server.tool()
    def get_step(run_id: str, seq: int) -> dict[str, Any]:
        """See what one tool call in a run actually sent and received.

        Use this when a workflow crashed on a field that was not there, or when
        a tool declares no outputSchema and you are writing code against a
        shape you had to guess. `run_id` comes back from run_workflow and
        dry_run_workflow; `seq` is the step number in their `steps` list.

        The result is trimmed: long lists are cut to their first few items and
        long strings to their first few hundred characters, and `trimmed` says
        what was dropped and from where. That is enough to see the shape and
        write correct code against it, which is what this is for. There is no
        way to ask for the whole payload -- reading a thousand rows is the
        workflow's job, in the sandbox, and `result_chars` tells you how much
        it would have been.

        On failure `code` is unknown-run or unknown-step.
        """
        with session() as conn:
            return _get_step(conn, run_id, seq)

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

    Over HTTP this process is a daemon, so it also drains the queue. Over stdio
    it is not: it lives and dies with one MCP host, and a queued run picked up
    here would be killed the moment that host went away.
    """
    if port is None:
        build_server(paths).run(transport="stdio")
    else:
        build_server(paths, drain=True).run(
            transport="streamable-http", host=host, port=port
        )
