"""What runs inside the runner subprocess.

This file is copied into a throwaway directory and executed there by an isolated
interpreter (``python -I``) with an empty environment. It therefore imports
nothing but the standard library and nothing from ``runlace`` -- there is a test
that checks it. It is the only Python the workflow shares its process with.

Two channels reach the outside:

* the workflow file and an inputs file, both named on the command line;
* a JSON-RPC pipe on the inherited stdin/stdout, over which every
  ``ctx.<connector>.<tool>(**kwargs)`` call is sent to the Runlace process,
  which performs the real MCP call and sends the result back.

The workflow's own stdout is pointed at stderr before it runs, so a stray
``print`` cannot land in the middle of a message.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from types import ModuleType
from typing import Any, TextIO

# Kept in step with runlace.runner, which cannot import this module (it copies
# it into an interpreter that has no access to the package). A test asserts the
# two agree.
METHOD_CALL_TOOL = "call_tool"
METHOD_FINISHED = "finished"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
RUN_FUNCTION = "run"
TYPES_MODULE = "runlace_types"

# The import allowlist ships with the runner. These are imported here, and then
# sys.path is emptied, so the workflow cannot reach the site-packages the parent
# process happens to have -- Runlace's own package and its MCP clients included.
# Kept in step with runlace.lint.ALLOWED_IMPORTS; a test checks that they agree.
ALLOWED_MODULES = (
    "collections",
    "collections.abc",
    "dataclasses",
    "datetime",
    "decimal",
    "itertools",
    "json",
    "math",
    "re",
    "statistics",
    "typing",
)


class ToolCallError(Exception):
    """A tool call the Runlace process could not complete."""


class Channel:
    """The JSON-RPC pipe back to the Runlace process."""

    def __init__(self, reader: TextIO, writer: TextIO) -> None:
        self._reader = reader
        self._writer = writer
        self._next_id = 0

    def call(self, connector: str, tool: str, arguments: dict[str, Any]) -> Any:
        self._next_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": METHOD_CALL_TOOL,
            "params": {"connector": connector, "tool": tool, "arguments": arguments},
        }
        try:
            line = json.dumps(request)
        except (TypeError, ValueError) as exc:
            raise ToolCallError(
                f"ctx.{connector}.{tool}: the arguments are not JSON ({exc})"
            ) from None

        self._write(line)
        response = self._read()
        error = response.get("error")
        if error is not None:
            raise ToolCallError(str(error.get("message", "the tool call failed")))
        return response.get("result")

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}))

    def _write(self, line: str) -> None:
        self._writer.write(line + "\n")
        self._writer.flush()

    def _read(self) -> dict[str, Any]:
        line = self._reader.readline()
        if not line:
            raise ToolCallError("the Runlace process closed the connection")
        message = json.loads(line)
        if not isinstance(message, dict):
            raise ToolCallError("the Runlace process sent something unexpected")
        return message


class Tool:
    """One bound ``ctx.<connector>.<tool>``. Keyword arguments only, as the stubs say."""

    def __init__(self, channel: Channel, connector: str, tool: str) -> None:
        self._channel = channel
        self._connector = connector
        self._tool = tool

    def __call__(self, **arguments: Any) -> Any:
        return self._channel.call(self._connector, self._tool, arguments)

    def __repr__(self) -> str:
        return f"<tool ctx.{self._connector}.{self._tool}>"


class Connector:
    def __init__(self, channel: Channel, name: str) -> None:
        self._channel = channel
        self._name = name

    def __getattr__(self, tool: str) -> Tool:
        if tool.startswith("_"):
            raise AttributeError(tool)
        return Tool(self._channel, self._name, tool)

    def __repr__(self) -> str:
        return f"<connector ctx.{self._name}>"


class Ctx:
    """What ``run(ctx)`` receives.

    Connector attributes resolve through ``__getattr__`` (D2); nothing is
    resolved until it is called, and nothing here knows what an MCP server is.
    """

    def __init__(self, channel: Channel, inputs: dict[str, Any]) -> None:
        self.inputs = inputs
        self._channel = channel

    def __getattr__(self, name: str) -> Connector:
        if name.startswith("_"):
            raise AttributeError(name)
        return Connector(self._channel, name)


def take_over_stdio() -> tuple[TextIO, TextIO]:
    """Move the pipe out of the workflow's reach and return ``(reader, writer)``.

    The workflow shares this process, so a ``print`` would corrupt a message and
    an ``input()`` would eat a response. Both ends of the pipe are duplicated
    onto private file objects; fd 1 is then pointed at stderr and fd 0 at
    ``/dev/null``.
    """
    reader = os.fdopen(os.dup(0), "r", encoding="utf-8")
    writer = os.fdopen(os.dup(1), "w", encoding="utf-8")

    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    os.dup2(2, 1)
    sys.stdout = sys.stderr

    return reader, writer


def seal_imports() -> None:
    """Import the allowlist, then close the door behind it.

    ``python -I`` gives us an empty environment and keeps the script's directory
    off ``sys.path``, but it leaves the site-packages of whatever interpreter
    spawned us: in a virtualenv that puts Runlace itself, its database layer and
    its MCP clients one ``import`` away. Emptying ``sys.path`` fixes that, and
    emptying ``sys.meta_path`` covers the rest -- ``os``, ``posix`` and friends
    are frozen or built into the interpreter and would still be reachable with
    no path at all.

    What survives is what is already in ``sys.modules``: the allowlist, plus the
    handful of modules the interpreter loaded to start up. This is not a sandbox
    -- D4 puts real isolation out of scope, and anything sharing a process can
    be reached through the object graph -- it is the same allowlist lint
    enforces at create time, enforced again where it happens to be cheap.
    """
    for name in ALLOWED_MODULES:
        __import__(name)
    import linecache  # tracebacks read the workflow file back through it

    linecache.checkcache()
    sys.path.clear()
    sys.meta_path.clear()
    for name in ("os", "os.path", "posix", "site", "zipimport"):
        sys.modules.pop(name, None)


def install_types_module() -> None:
    """Make ``from runlace_types import Ctx, Output`` work at run time.

    The generated package is stubs only -- ``.pyi`` files with no code behind
    them -- so the import a workflow has to start with would fail on its own.
    Registering the module directly, rather than putting a package on
    ``sys.path``, keeps the path empty of anything the workflow could reach.

    ``Inputs`` and ``Output`` are plain ``dict`` here. Their real shapes were
    enforced by pyright at create time and are checked again against the
    declared schemas by the Runlace process; the runner does not need them.
    """
    module = ModuleType(TYPES_MODULE)
    module.__dict__.update({"Ctx": Ctx, "Inputs": dict, "Output": dict})
    sys.modules[TYPES_MODULE] = module


def load_run(workflow_path: str) -> Any:
    """Execute the workflow file and hand back its ``run`` function.

    Compiled against its real path so that a traceback points at the line the
    agent wrote.
    """
    install_types_module()
    with open(workflow_path, encoding="utf-8") as handle:
        source = handle.read()
    namespace: dict[str, Any] = {"__name__": "workflow", "__file__": workflow_path}
    exec(compile(source, workflow_path, "exec"), namespace)  # noqa: S102
    run = namespace.get(RUN_FUNCTION)
    if not callable(run):
        raise TypeError(f"the workflow does not define a callable `{RUN_FUNCTION}`")
    return run


def describe(exc: BaseException, workflow_path: str) -> dict[str, Any]:
    """Turn an exception into something the agent can read.

    Only frames from the workflow file are kept: the shim's own frames are an
    implementation detail, and showing them would send the agent looking in the
    wrong file. The last line is built by hand rather than taken from
    ``format_exception_only`` for the same reason -- that would spell the
    exception with the shim's module in front of it.
    """
    frames = [
        frame
        for frame in traceback.extract_tb(exc.__traceback__)
        if frame.filename == workflow_path
    ]
    name = type(exc).__name__
    message = str(exc) or name
    lines = traceback.format_list(frames) if frames else []
    lines.append(f"{name}: {message}")
    return {
        "type": name,
        "message": message,
        "line": frames[-1].lineno if frames else None,
        "traceback": "".join(lines).strip(),
    }


def main(argv: list[str]) -> int:
    workflow_path, inputs_path = argv[1], argv[2]
    reader, writer = take_over_stdio()
    channel = Channel(reader, writer)

    with open(inputs_path, encoding="utf-8") as handle:
        inputs = json.load(handle)

    seal_imports()

    status: str = STATUS_FAILED
    output: Any = None
    error: dict[str, Any] | None = None
    try:
        output = load_run(workflow_path)(Ctx(channel, inputs))
        status = STATUS_COMPLETED
    except BaseException as exc:  # noqa: BLE001 - every failure is a run result
        error = describe(exc, workflow_path)

    try:
        json.dumps(output)
    except (TypeError, ValueError) as exc:
        status, output = STATUS_FAILED, None
        error = {
            "type": "TypeError",
            "message": f"the workflow returned something that is not JSON ({exc})",
            "line": None,
            "traceback": None,
        }

    channel.notify(
        METHOD_FINISHED, {"status": status, "output": output, "error": error}
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
