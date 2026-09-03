"""The code that shares a process with the workflow.

The shim is copied into an interpreter that cannot see the `runlace` package, so
the first test here is a structural one: it must stay standard-library only, and
the constants it shares with :mod:`runlace.runner` must stay in step, because
nothing at import time will catch it if they drift.

`seal_imports` is deliberately not called in-process -- it empties `sys.path`.
It is exercised for real in test_runner.py, inside a subprocess.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any

import pytest

from runlace import runner, runner_shim
from runlace.lint import ALLOWED_IMPORTS
from runlace.runner_shim import (
    ALLOWED_MODULES,
    Channel,
    Ctx,
    ToolCallError,
    describe,
    install_types_module,
    load_run,
)

SOURCE = Path(str(runner_shim.__file__)).read_text(encoding="utf-8")


class FakeChannel:
    """Stands in for the pipe: records calls, replays canned answers."""

    def __init__(self, answers: list[Any] | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.answers = list(answers or [])

    def call(self, connector: str, tool: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((connector, tool, arguments))
        return self.answers.pop(0) if self.answers else None


# -- structural guards -----------------------------------------------------


def test_the_shim_imports_nothing_from_runlace() -> None:
    """It runs where the package does not exist. An import here is a crash there."""
    tree = ast.parse(SOURCE)
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "the shim cannot use relative imports"
            modules.append(node.module or "")
    assert all(not m.startswith("runlace") for m in modules), modules
    assert all(m.split(".")[0] in sys.stdlib_module_names for m in modules), modules


def test_the_allowlist_matches_the_one_lint_enforces() -> None:
    roots = {name.split(".")[0] for name in ALLOWED_MODULES}
    assert roots == set(ALLOWED_IMPORTS)


def test_the_protocol_constants_are_shared_with_the_parent() -> None:
    assert runner.STATUS_COMPLETED == runner_shim.STATUS_COMPLETED
    assert runner.STATUS_FAILED == runner_shim.STATUS_FAILED


# -- ctx -------------------------------------------------------------------


def test_ctx_turns_attribute_access_into_a_tool_call() -> None:
    channel = FakeChannel(answers=[{"balance": 12}])
    ctx = Ctx(channel, {"year": 2024})  # type: ignore[arg-type]

    assert ctx.pennylane.get_balance() == {"balance": 12}
    assert channel.calls == [("pennylane", "get_balance", {})]
    assert ctx.inputs == {"year": 2024}


def test_arguments_are_passed_through_by_keyword() -> None:
    channel = FakeChannel()
    ctx = Ctx(channel, {})  # type: ignore[arg-type]
    ctx.pennylane.list_transactions(from_="a", to="b")
    assert channel.calls == [("pennylane", "list_transactions", {"from_": "a", "to": "b"})]


def test_nothing_is_resolved_until_it_is_called() -> None:
    """`ctx.<anything>` is legal at run time; create_workflow is what rejects typos."""
    channel = FakeChannel()
    ctx = Ctx(channel, {})  # type: ignore[arg-type]
    tool = ctx.nosuch.thing
    assert channel.calls == []
    assert "ctx.nosuch.thing" in repr(tool)


def test_private_attributes_are_not_connectors() -> None:
    ctx = Ctx(FakeChannel(), {})  # type: ignore[arg-type]
    with pytest.raises(AttributeError):
        _ = ctx._channel.anything  # type: ignore[attr-defined]


# -- the channel -----------------------------------------------------------


class Pipe:
    """A one-shot in-memory stand-in for the JSON-RPC pipe."""

    def __init__(self, response: str) -> None:
        self.written: list[str] = []
        self._response = response

    def write(self, text: str) -> None:
        self.written.append(text)

    def flush(self) -> None:
        pass

    def readline(self) -> str:
        return self._response


def channel_for(response: str) -> tuple[Channel, Pipe]:
    pipe = Pipe(response)
    return Channel(pipe, pipe), pipe  # type: ignore[arg-type]


def test_a_call_is_one_line_of_json_rpc() -> None:
    channel, pipe = channel_for('{"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n')
    assert channel.call("gmail", "send_email", {"to": "a"}) == {"ok": True}
    assert pipe.written[0].endswith("\n")
    assert '"method": "call_tool"' in pipe.written[0]


def test_an_error_response_raises_in_the_workflow() -> None:
    channel, _ = channel_for('{"jsonrpc":"2.0","id":1,"error":{"message":"boom"}}\n')
    with pytest.raises(ToolCallError, match="boom"):
        channel.call("gmail", "send_email", {})


def test_a_closed_pipe_raises_rather_than_hanging() -> None:
    channel, _ = channel_for("")
    with pytest.raises(ToolCallError, match="closed the connection"):
        channel.call("gmail", "send_email", {})


def test_arguments_that_are_not_json_are_refused_before_they_are_sent() -> None:
    channel, pipe = channel_for("")
    with pytest.raises(ToolCallError, match="not JSON"):
        channel.call("gmail", "send_email", {"body": object()})
    assert pipe.written == []


# -- loading the workflow --------------------------------------------------


@pytest.fixture
def types_module() -> Any:
    """Install the synthetic `runlace_types` and take it away again."""
    install_types_module()
    yield
    sys.modules.pop("runlace_types", None)


def test_the_types_import_a_workflow_starts_with_works(
    tmp_path: Path, types_module: None
) -> None:
    """The generated package is stubs only; at run time the shim provides it."""
    path = tmp_path / "workflow.py"
    path.write_text(
        "from runlace_types import Ctx, Output\n\n\n"
        "def run(ctx: Ctx) -> Output:\n"
        '    return {"n": ctx.inputs["n"]}\n',
        encoding="utf-8",
    )
    run = load_run(str(path))
    assert run(Ctx(FakeChannel(), {"n": 3})) == {"n": 3}  # type: ignore[arg-type]


def test_a_file_without_run_is_rejected(tmp_path: Path, types_module: None) -> None:
    path = tmp_path / "workflow.py"
    path.write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(TypeError, match="callable `run`"):
        load_run(str(path))


# -- describing a crash ----------------------------------------------------


def test_a_crash_points_at_the_workflows_own_line(
    tmp_path: Path, types_module: None
) -> None:
    path = tmp_path / "workflow.py"
    path.write_text(
        "from runlace_types import Ctx\n\n\n"
        "def run(ctx: Ctx) -> dict[str, object]:\n"
        "    return {}['missing']\n",
        encoding="utf-8",
    )
    run = load_run(str(path))
    with pytest.raises(KeyError) as raised:
        run(Ctx(FakeChannel(), {}))  # type: ignore[arg-type]
    detail = describe(raised.value, str(path))

    assert detail["type"] == "KeyError"
    assert detail["line"] == 5
    assert "line 5, in run" in detail["traceback"]


def test_the_shims_own_frames_are_not_shown(tmp_path: Path, types_module: None) -> None:
    """A traceback through the shim would send the agent looking in the wrong file."""
    path = tmp_path / "workflow.py"
    path.write_text(
        "from runlace_types import Ctx\n\n\n"
        "def run(ctx: Ctx) -> dict[str, object]:\n"
        "    ctx.gmail.send_email()\n"
        "    return {}\n",
        encoding="utf-8",
    )
    channel, _ = channel_for('{"jsonrpc":"2.0","id":1,"error":{"message":"boom"}}\n')
    with pytest.raises(ToolCallError) as raised:
        load_run(str(path))(Ctx(channel, {}))
    detail = describe(raised.value, str(path))

    assert detail["message"] == "boom"
    assert "runner_shim" not in detail["traceback"]
    assert detail["traceback"].count("File ") == 1


def test_an_exception_with_no_message_still_has_one() -> None:
    detail = describe(ValueError(), "/nowhere/workflow.py")
    assert detail["message"] == "ValueError"
    assert detail["line"] is None
