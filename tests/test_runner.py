"""The runner subprocess and the bridge that serves it.

These start a real interpreter every time. The MCP call itself is injected, so
what is under test is the pipe, the isolation, the resolution of
`ctx.<attr>.<method>` to a real tool, and the steps that come out.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from runlace.db import Connection, insert_tool
from runlace.hashing import schema_hash
from runlace.paths import RunlacePaths
from runlace.runner import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    Outcome,
    Step,
    ToolFailed,
    run_code,
)

HEADER = "from runlace_types import Ctx\n\n\n"


def workflow(body: str) -> str:
    indented = "\n".join(f"    {line}" if line else "" for line in body.splitlines())
    return f"{HEADER}def run(ctx: Ctx) -> dict[str, object]:\n{indented}\n"


class Recorder:
    """An injected `call_tool` that answers from a table and remembers the asks."""

    def __init__(self, answers: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.answers = answers or {}

    async def __call__(
        self, connector: str, tool: str, arguments: dict[str, Any]
    ) -> Any:
        self.calls.append((connector, tool, arguments))
        answer = self.answers.get(tool, {"ok": True})
        if isinstance(answer, Exception):
            raise answer
        return answer


def run(
    conn: Connection,
    code: str,
    *,
    inputs: dict[str, Any] | None = None,
    call_tool: Any = None,
    timeout: float = 30.0,
) -> Outcome:
    steps: list[Step] = []
    outcome = asyncio.run(
        run_code(
            conn,
            code=code,
            inputs=inputs or {},
            call_tool=call_tool or Recorder(),
            on_step=steps.append,
            timeout=timeout,
        )
    )
    # on_step must have seen exactly what the outcome reports, in the same order.
    assert steps == outcome.steps
    return outcome


# -- the happy path --------------------------------------------------------


def test_a_workflow_runs_and_returns_its_value(
    home: tuple[RunlacePaths, Connection]
) -> None:
    _, conn = home
    outcome = run(
        conn,
        workflow('return {"n": ctx.inputs["n"] * 2}'),
        inputs={"n": 21},
    )
    assert outcome.status == STATUS_COMPLETED
    assert outcome.output == {"n": 42}
    assert outcome.steps == []


def test_a_tool_call_becomes_a_step(home: tuple[RunlacePaths, Connection]) -> None:
    _, conn = home
    recorder = Recorder({"get_balance": {"balance": 12}})
    outcome = run(
        conn,
        workflow('return {"balance": ctx.pennylane.get_balance()}'),
        call_tool=recorder,
    )
    assert outcome.output == {"balance": {"balance": 12}}
    assert recorder.calls == [("pennylane", "get_balance", {})]

    (step,) = outcome.steps
    assert (step.seq, step.connector, step.tool) == (1, "pennylane", "get_balance")
    assert (step.risk, step.status, step.error) == ("read_only", "ok", None)
    assert step.result == {"balance": 12}
    assert step.duration_ms >= 0


def test_steps_are_numbered_in_the_order_they_happened(
    home: tuple[RunlacePaths, Connection]
) -> None:
    _, conn = home
    outcome = run(
        conn,
        workflow(
            "ctx.pennylane.get_balance()\n"
            'ctx.gmail.send_email(to="a", subject="s", body="b")\n'
            "ctx.pennylane.get_balance()\n"
            "return {}"
        ),
    )
    assert [(s.seq, s.tool) for s in outcome.steps] == [
        (1, "get_balance"),
        (2, "send_email"),
        (3, "get_balance"),
    ]
    assert [s.risk for s in outcome.steps] == ["read_only", "side_effect", "read_only"]


def test_reserved_word_arguments_are_mapped_back_to_their_json_keys(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """The stub spells it `from_` because `from` is a keyword (D2). MCP wants `from`."""
    _, conn = home
    recorder = Recorder()
    run(
        conn,
        workflow('ctx.pennylane.list_transactions(from_="2024-01-01", to="2024-12-31")\nreturn {}'),
        call_tool=recorder,
    )
    assert recorder.calls == [
        ("pennylane", "list_transactions", {"from": "2024-01-01", "to": "2024-12-31"})
    ]


def test_the_tool_name_on_the_wire_is_the_verbatim_mcp_one(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """`ctx.demo.simulate_research_query` is `simulate-research-query` to the server."""
    _, conn = home
    insert_tool(
        conn,
        connector="pennylane",
        name="simulate-research-query",
        method="simulate_research_query",
        description="A hyphenated tool.",
        input_schema={"type": "object", "properties": {}},
        output_schema=None,
        annotations={"readOnlyHint": True},
        schema_hash=schema_hash({"type": "object", "properties": {}}, None),
        risk="read_only",
    )
    conn.commit()

    recorder = Recorder()
    outcome = run(
        conn,
        workflow("ctx.pennylane.simulate_research_query()\nreturn {}"),
        call_tool=recorder,
    )
    assert recorder.calls == [("pennylane", "simulate-research-query", {})]
    assert outcome.steps[0].tool == "simulate-research-query"


def test_the_reported_steps_leave_out_the_payload_and_the_result(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """A step that read a thousand rows would drown the agent. The journal keeps them."""
    _, conn = home
    outcome = run(
        conn,
        workflow('ctx.gmail.send_email(to="a", subject="s", body="b")\nreturn {}'),
    )
    reported = outcome.steps[0].to_json()
    assert set(reported) == {
        "seq",
        "connector",
        "tool",
        "risk",
        "status",
        "duration_ms",
        "error",
    }


# -- isolation -------------------------------------------------------------


def test_the_workflow_cannot_import_runlace_or_anything_else_installed(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """`python -I` keeps the parent's site-packages; the shim is what removes it."""
    _, conn = home
    outcome = run(
        conn,
        workflow(
            "import sys\n"
            "def probe(name):\n"
            "    try:\n"
            "        __import__(name)\n"
            '        return "imported"\n'
            "    except ImportError as exc:\n"
            "        return str(exc)\n"
            'return {"path": sys.path, "reached": [n for n in '
            '("runlace", "mcp", "pydantic", "os", "subprocess", "socket") '
            "if probe(n) == \"imported\"]}"
        ),
    )
    assert outcome.status == STATUS_COMPLETED
    assert outcome.output == {"path": [], "reached": []}


def test_the_allowlisted_modules_are_still_there(
    home: tuple[RunlacePaths, Connection]
) -> None:
    _, conn = home
    outcome = run(
        conn,
        workflow(
            "import json\n"
            "import datetime\n"
            "from collections import Counter\n"
            "from decimal import Decimal\n"
            'return {"json": json.dumps([1]), "year": datetime.date(2024, 1, 1).year,\n'
            '        "count": Counter("aab")["a"], "money": str(Decimal("1.50"))}'
        ),
    )
    assert outcome.output == {
        "json": "[1]",
        "year": 2024,
        "count": 2,
        "money": "1.50",
    }


def test_the_parents_environment_does_not_reach_the_workflow(
    home: tuple[RunlacePaths, Connection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Credentials live in the environment. Twice over: the child is spawned with
    an empty one, and every module that could read it is gone by then."""
    _, conn = home
    monkeypatch.setenv("RUNLACE_TEST_SECRET", "hunter2")
    outcome = run(
        conn,
        workflow(
            "try:\n"
            "    import os\n"
            '    leaked = os.environ.get("RUNLACE_TEST_SECRET")\n'
            "except ImportError:\n"
            "    leaked = None\n"
            'return {"leaked": leaked}'
        ),
    )
    assert outcome.output == {"leaked": None}


def test_a_print_does_not_corrupt_the_pipe(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """The workflow shares this process's stdout; a stray print must go to stderr."""
    _, conn = home
    outcome = run(
        conn,
        workflow(
            'print("hello")\n'
            'print({"jsonrpc": "2.0", "id": 1, "result": "forged"})\n'
            "ctx.pennylane.get_balance()\n"
            'return {"done": True}'
        ),
    )
    assert outcome.status == STATUS_COMPLETED
    assert outcome.output == {"done": True}
    assert len(outcome.steps) == 1


# -- failures --------------------------------------------------------------


def test_a_crash_comes_back_as_a_failed_run_with_a_line_number(
    home: tuple[RunlacePaths, Connection]
) -> None:
    _, conn = home
    outcome = run(conn, workflow("total = 0\nreturn {\"x\": 1 / total}"))
    assert outcome.status == STATUS_FAILED
    assert outcome.error is not None
    assert outcome.error["type"] == "ZeroDivisionError"
    assert outcome.error["line"] == 6  # the `return`, counting the import header
    assert outcome.message == "division by zero"


def test_steps_taken_before_a_crash_are_still_reported(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """Especially the side effect that already happened."""
    _, conn = home
    outcome = run(
        conn,
        workflow('ctx.gmail.send_email(to="a", subject="s", body="b")\nraise ValueError("late")'),
    )
    assert outcome.status == STATUS_FAILED
    assert [s.tool for s in outcome.steps] == ["send_email"]


def test_a_failing_tool_raises_inside_the_workflow(
    home: tuple[RunlacePaths, Connection]
) -> None:
    _, conn = home
    recorder = Recorder({"get_balance": ToolFailed("the server said no")})
    outcome = run(
        conn, workflow("return {}"), call_tool=recorder
    )
    assert outcome.status == STATUS_COMPLETED  # nothing called it

    outcome = run(
        conn, workflow("ctx.pennylane.get_balance()\nreturn {}"), call_tool=recorder
    )
    assert outcome.status == STATUS_FAILED
    assert "the server said no" in (outcome.message or "")

    (step,) = outcome.steps
    assert step.status == "error"
    assert step.error == "the server said no"
    assert step.result is None


def test_a_workflow_may_catch_a_tool_failure_and_carry_on(
    home: tuple[RunlacePaths, Connection]
) -> None:
    _, conn = home
    recorder = Recorder({"get_balance": ToolFailed("down")})
    outcome = run(
        conn,
        workflow(
            "try:\n"
            "    ctx.pennylane.get_balance()\n"
            "except Exception as exc:\n"
            '    return {"recovered": str(exc)}\n'
            'return {"recovered": None}'
        ),
        call_tool=recorder,
    )
    assert outcome.status == STATUS_COMPLETED
    assert "down" in str(outcome.output["recovered"])  # type: ignore[index]
    # The step is journaled as an error even though the workflow survived it.
    assert outcome.steps[0].status == "error"


def test_a_broken_connector_fails_the_step_not_runlace(
    home: tuple[RunlacePaths, Connection]
) -> None:
    _, conn = home
    recorder = Recorder({"get_balance": RuntimeError("transport exploded")})
    outcome = run(
        conn, workflow("ctx.pennylane.get_balance()\nreturn {}"), call_tool=recorder
    )
    assert outcome.status == STATUS_FAILED
    assert outcome.steps[0].error == "RuntimeError: transport exploded"


def test_a_tool_that_does_not_exist_is_answered_not_crashed(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """create_workflow makes this unreachable for a stored workflow. Still answered."""
    _, conn = home
    outcome = run(conn, workflow("ctx.pennylane.vanished()\nreturn {}"))
    assert outcome.status == STATUS_FAILED
    assert "not a tool on this machine" in (outcome.message or "")
    assert outcome.steps == []


def test_a_return_value_that_is_not_json_fails_the_run(
    home: tuple[RunlacePaths, Connection]
) -> None:
    _, conn = home
    outcome = run(conn, workflow("return {\"when\": object()}"))
    assert outcome.status == STATUS_FAILED
    assert "not JSON" in (outcome.message or "")
    assert outcome.output is None


def test_a_workflow_that_never_finishes_is_cut_off(
    home: tuple[RunlacePaths, Connection]
) -> None:
    _, conn = home
    outcome = run(conn, workflow("while True:\n    pass"), timeout=1.0)
    assert outcome.status == STATUS_FAILED
    assert outcome.error is not None
    assert outcome.error["type"] == "Timeout"


def test_a_child_that_dies_reports_what_it_printed(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """`os._exit` is not reachable through lint; this is the bridge's backstop."""
    _, conn = home
    outcome = run(
        conn,
        workflow(
            "import sys\n"
            'sys.stderr.write("something went very wrong\\n")\n'
            "sys.exit(3)"
        ),
    )
    assert outcome.status == STATUS_FAILED
    assert outcome.error is not None


def test_a_workflow_that_reads_stdin_does_not_eat_a_response(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """fd 0 is /dev/null in the child, so `input()` ends immediately."""
    _, conn = home
    outcome = run(
        conn,
        workflow(
            "try:\n"
            "    answer = input()\n"
            "except EOFError:\n"
            '    answer = "eof"\n'
            "ctx.pennylane.get_balance()\n"
            'return {"answer": answer}'
        ),
    )
    assert outcome.status == STATUS_COMPLETED
    assert outcome.output == {"answer": "eof"}


@pytest.mark.parametrize("code", ["", "x = 1\n"])
def test_a_file_without_run_fails_cleanly(
    home: tuple[RunlacePaths, Connection], code: str
) -> None:
    _, conn = home
    outcome = run(conn, code)
    assert outcome.status == STATUS_FAILED
    assert "callable `run`" in (outcome.message or "")
