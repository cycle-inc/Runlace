"""Spawning the runner subprocess and serving its tool calls.

The child runs the workflow in an isolated interpreter; this is the other end of
its pipe. For every ``ctx.<attr>.<method>(**kwargs)`` it asks for, this module
resolves the connector and the verbatim MCP tool name, translates the arguments
out of the stubs' spelling and back into the schema's (``from_`` -> ``from``, at
every level -- see :mod:`runlace.keys`), performs the call through the injected
``call_tool``, records a step, and translates the result the other way.

The MCP call itself is injected rather than imported so that the bridge can be
tested without a live server, and so that the only module talking to MCP servers
stays :mod:`runlace.sessions`.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any, Awaitable, Callable

from . import runner_shim
from .db import Connection, find_tool_by_method
from .keys import to_json_keys, to_python_keys

RUNNER_FILENAME = "_runlace_runner.py"
WORKFLOW_FILENAME = "workflow.py"
INPUTS_FILENAME = "inputs.json"

# Wall clock for the whole run, and for any single tool call. Neither is in the
# spec; without them a workflow that loops forever, or a server that never
# answers, would hang the agent that called run_workflow.
DEFAULT_TIMEOUT_SECONDS = 300.0
DEFAULT_TOOL_TIMEOUT_SECONDS = 120.0

STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STEP_OK = "ok"
STEP_ERROR = "error"

# How much of the child's stderr to quote when it dies without answering.
_STDERR_TAIL = 2000


class ToolFailed(Exception):
    """A tool call that reached a server and came back as a failure."""


# (connector name, verbatim tool name, arguments) -> whatever the tool returned.
CallTool = Callable[[str, str, dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class Step:
    """One tool call, as it will be journaled."""

    seq: int
    connector: str
    tool: str
    risk: str
    payload: dict[str, Any]
    result: Any
    status: str
    duration_ms: int
    error: str | None

    def to_json(self) -> dict[str, Any]:
        """What `run_workflow` reports.

        Deliberately without ``payload`` and ``result``: a step that read a
        thousand rows would drown the agent's context. Both are in the journal,
        which is where a debug trace belongs.
        """
        return {
            "seq": self.seq,
            "connector": self.connector,
            "tool": self.tool,
            "risk": self.risk,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "error": self.error,
        }


@dataclass
class Outcome:
    status: str
    output: Any = None
    error: dict[str, Any] | None = None
    steps: list[Step] = field(default_factory=list[Step])

    @property
    def ok(self) -> bool:
        return self.status == STATUS_COMPLETED

    @property
    def message(self) -> str | None:
        if self.error is None:
            return None
        return str(self.error.get("message") or self.error.get("type") or "run failed")


async def run_code(
    conn: Connection,
    *,
    code: str,
    inputs: dict[str, Any],
    call_tool: CallTool,
    on_step: Callable[[Step], None] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Outcome:
    """Run one workflow to completion and return what happened.

    Never raises for anything the workflow does: a crash, a bad return value and
    a tool failure are all outcomes.
    """
    root = Path(tempfile.mkdtemp(prefix="runlace-run-"))
    steps: list[Step] = []
    try:
        _stage(root, code=code, inputs=inputs)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            # -I: no environment, no user site, and the script's directory is
            # not prepended to sys.path. The staged directory holds nothing but
            # the shim and the workflow anyway.
            "-I",
            RUNNER_FILENAME,
            WORKFLOW_FILENAME,
            INPUTS_FILENAME,
            cwd=root,
            env={},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return await _serve(
            process,
            conn,
            call_tool=call_tool,
            steps=steps,
            on_step=on_step,
            timeout=timeout,
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _stage(root: Path, *, code: str, inputs: dict[str, Any]) -> None:
    """Lay out the run directory: the shim, the workflow, the inputs. Nothing else."""
    shim = Path(str(runner_shim.__file__))
    shutil.copyfile(shim, root / RUNNER_FILENAME)
    (root / WORKFLOW_FILENAME).write_text(code, encoding="utf-8")
    (root / INPUTS_FILENAME).write_text(json.dumps(inputs), encoding="utf-8")


async def _serve(
    process: asyncio.subprocess.Process,
    conn: Connection,
    *,
    call_tool: CallTool,
    steps: list[Step],
    on_step: Callable[[Step], None] | None,
    timeout: float,
) -> Outcome:
    """Answer the child until it reports a result, dies, or runs out of time."""
    assert process.stdout is not None and process.stdin is not None
    assert process.stderr is not None

    # Drained concurrently: a child that writes more to stderr than the pipe
    # holds would block forever if nobody were reading it.
    stderr_task = asyncio.ensure_future(process.stderr.read())
    stderr_text = ""
    died = False
    deadline = perf_counter() + timeout

    try:
        while True:
            remaining = deadline - perf_counter()
            if remaining <= 0:
                return _timed_out(steps, timeout)
            try:
                line = await asyncio.wait_for(process.stdout.readline(), remaining)
            except asyncio.TimeoutError:
                return _timed_out(steps, timeout)

            if not line:
                died = True
                break

            message = _parse(line)
            if message is None:
                continue

            if message.get("method") == runner_shim.METHOD_FINISHED:
                params = message.get("params") or {}
                return Outcome(
                    status=str(params.get("status") or STATUS_FAILED),
                    output=params.get("output"),
                    error=params.get("error"),
                    steps=steps,
                )

            response = await _respond(
                message, conn, call_tool=call_tool, steps=steps, on_step=on_step
            )
            process.stdin.write((json.dumps(response) + "\n").encode("utf-8"))
            await process.stdin.drain()
    finally:
        await _terminate(process)
        if died:
            stderr_text = (await stderr_task).decode("utf-8", "replace")
        else:
            stderr_task.cancel()
            with suppress(asyncio.CancelledError):
                await stderr_task

    return Outcome(
        status=STATUS_FAILED,
        error={
            "type": "RunnerDied",
            "message": "the workflow process exited without returning a result",
            "line": None,
            "traceback": stderr_text[-_STDERR_TAIL:].strip() or None,
        },
        steps=steps,
    )


def _timed_out(steps: list[Step], timeout: float) -> Outcome:
    return Outcome(
        status=STATUS_FAILED,
        error={
            "type": "Timeout",
            "message": f"the workflow did not finish within {timeout:.0f}s",
            "line": None,
            "traceback": None,
        },
        steps=steps,
    )


def _parse(line: bytes) -> dict[str, Any] | None:
    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        return None
    return message if isinstance(message, dict) else None


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        process.kill()
    await process.wait()


async def _respond(
    message: dict[str, Any],
    conn: Connection,
    *,
    call_tool: CallTool,
    steps: list[Step],
    on_step: Callable[[Step], None] | None,
) -> dict[str, Any]:
    """Perform one requested tool call and build the JSON-RPC reply."""
    request_id = message.get("id")
    if message.get("method") != runner_shim.METHOD_CALL_TOOL:
        return _error(request_id, f"unsupported request `{message.get('method')}`")

    params = message.get("params") or {}
    attr = str(params.get("connector", ""))
    method = str(params.get("tool", ""))
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}

    row = find_tool_by_method(conn, attr, method)
    if row is None:
        # Unreachable for a stored workflow: create_workflow resolved every call
        # site, and the drift gate refuses to run one whose tools have since
        # disappeared. Answered rather than crashed, all the same.
        return _error(request_id, f"`ctx.{attr}.{method}` is not a tool on this machine")

    connector = str(row["connector"])
    tool = str(row["tool"])
    payload = to_json_keys(arguments, _schema(row["input_schema_json"]))

    started = perf_counter()
    result: Any = None
    error: str | None = None
    try:
        result = await call_tool(connector, tool, payload)
    except ToolFailed as exc:
        error = str(exc)
    except Exception as exc:  # noqa: BLE001 - a broken connector fails the step, not Runlace
        error = f"{type(exc).__name__}: {exc}"

    step = Step(
        seq=len(steps) + 1,
        connector=connector,
        tool=tool,
        risk=str(row["risk"]),
        payload=payload,
        result=result,
        status=STEP_ERROR if error else STEP_OK,
        duration_ms=int((perf_counter() - started) * 1000),
        error=error,
    )
    steps.append(step)
    if on_step is not None:
        on_step(step)

    if error is not None:
        return _error(request_id, f"{connector}.{tool}: {error}")
    # The journal keeps both sides in the server's spelling -- it is the record
    # of what went over the wire -- but the workflow gets back what its stub
    # promised, so `from` becomes `from_` again on the way in.
    answer = to_python_keys(result, _schema(row["output_schema_json"]))
    return {"jsonrpc": "2.0", "id": request_id, "result": answer}


def _schema(column: Any) -> dict[str, Any] | None:
    """A schema column as a dict, or ``None`` if it is absent or unreadable."""
    if not isinstance(column, str):
        return None
    try:
        schema = json.loads(column)
    except json.JSONDecodeError:
        return None
    return schema if isinstance(schema, dict) else None


def _error(request_id: Any, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"message": message}}
