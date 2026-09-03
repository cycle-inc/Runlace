"""Running a stored workflow: the gates, in order, and the journal.

The spec's order is the contract -- inputs, drift, confirm, run, output -- and so
is the rule that every attempt is journaled. A refusal that left no `runs` row
would be a refusal nobody could audit, so each gate here is checked twice: for
what it returns, and for what it wrote down.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from runlace.db import Connection, find_run, list_steps
from runlace.journal import MAX_ITEMS, get_step
from runlace.paths import RunlacePaths
from runlace.runner import ToolFailed
from runlace.runs import (
    CODE_INVALID_INPUTS,
    CODE_INVALID_OUTPUT,
    CODE_NEEDS_CONFIRMATION,
    CODE_SCHEMA_DRIFT,
    CODE_UNKNOWN_CONNECTOR,
    CODE_UNKNOWN_WORKFLOW,
    CODE_WORKFLOW_FAILED,
    run_workflow,
)
from runlace.workflows import create_workflow, list_workflows

INPUTS = {
    "type": "object",
    "properties": {"to": {"type": "string"}},
    "required": ["to"],
}

# Two reads and one send: M3's acceptance shape, in miniature.
TWO_READS_ONE_SEND = (
    "from runlace_types import Ctx\n\n\n"
    "def run(ctx: Ctx) -> dict[str, object]:\n"
    "    balance = ctx.pennylane.get_balance()\n"
    '    txs = ctx.pennylane.list_transactions(from_="2024-01-01", to="2024-12-31")\n'
    '    ctx.gmail.send_email(to=ctx.inputs["to"], subject="Report", body="hi")\n'
    '    return {"balance": balance, "count": len(txs["transactions"])}\n'
)

READ_ONLY = (
    "from runlace_types import Ctx\n\n\n"
    "def run(ctx: Ctx) -> dict[str, object]:\n"
    '    return {"balance": ctx.pennylane.get_balance(), "to": ctx.inputs["to"]}\n'
)

ANSWERS: dict[str, Any] = {
    "get_balance": {"balance": 1234.5},
    "list_transactions": {"transactions": [{"id": "a", "amount": 1.0}]},
    "send_email": {"sent": True},
}


class Recorder:
    """An injected `call_tool`, so these tests need no live MCP server."""

    def __init__(self, answers: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.answers = {**ANSWERS, **(answers or {})}

    async def __call__(
        self, connector: str, tool: str, arguments: dict[str, Any]
    ) -> Any:
        self.calls.append((connector, tool, arguments))
        answer = self.answers.get(tool)
        if isinstance(answer, Exception):
            raise answer
        return answer


def store(
    home: tuple[RunlacePaths, Connection],
    code: str = TWO_READS_ONE_SEND,
    *,
    name: str = "report",
    outputs_schema: dict[str, Any] | None = None,
) -> str:
    paths, conn = home
    result = create_workflow(
        conn,
        paths,
        name=name,
        description="A report.",
        code=code,
        inputs_schema=INPUTS,
        outputs_schema=outputs_schema,
    )
    assert result.ok, [str(e) for e in result.errors]
    return name


def execute(
    home: tuple[RunlacePaths, Connection],
    *,
    workflow: str = "report",
    inputs: dict[str, Any] | None = None,
    confirm: bool = False,
    version: str | None = None,
    dry_run: bool = False,
    call_tool: Any = None,
) -> dict[str, Any]:
    paths, conn = home
    return asyncio.run(
        run_workflow(
            conn,
            paths,
            workflow=workflow,
            inputs=inputs if inputs is not None else {"to": "a@b.c"},
            confirm=confirm,
            version=version,
            dry_run=dry_run,
            call_tool=call_tool or Recorder(),
            timeout=30.0,
        )
    )


def journal(conn: Connection, run_id: str) -> tuple[Any, list[Any]]:
    row = find_run(conn, run_id)
    assert row is not None, "every attempt must leave a run row"
    return row, list_steps(conn, run_id)


# -- the acceptance shape: refused, then confirmed -------------------------


def test_a_workflow_with_a_side_effect_is_refused_without_confirm(
    home: tuple[RunlacePaths, Connection]
) -> None:
    _, conn = home
    store(home)
    recorder = Recorder()
    result = execute(home, call_tool=recorder)

    assert result["ok"] is False
    assert result["code"] == CODE_NEEDS_CONFIRMATION
    assert result["side_effects"] == [{"connector": "gmail", "tool": "send_email"}]
    assert "confirm=True" in result["hint"]

    # Refused means refused: not one tool was called, not even a read.
    assert recorder.calls == []

    row, steps = journal(conn, result["run_id"])
    assert row["status"] == "failed"
    assert row["confirmed"] == 0
    assert "send_email" in row["error"]
    assert json.loads(row["inputs_json"]) == {"to": "a@b.c"}
    assert steps == []


def test_the_same_workflow_completes_with_confirm(
    home: tuple[RunlacePaths, Connection]
) -> None:
    _, conn = home
    store(home)
    recorder = Recorder()
    result = execute(home, confirm=True, call_tool=recorder)

    assert result["ok"] is True, result
    assert result["status"] == "completed"
    assert result["output"] == {"balance": {"balance": 1234.5}, "count": 1}
    assert [t for _, t, _ in recorder.calls] == [
        "get_balance",
        "list_transactions",
        "send_email",
    ]

    row, steps = journal(conn, result["run_id"])
    assert row["status"] == "completed"
    assert row["confirmed"] == 1
    assert row["error"] is None
    assert row["finished_at"] is not None
    assert json.loads(row["output_json"]) == result["output"]

    assert [(s["seq"], s["tool"], s["risk"]) for s in steps] == [
        (1, "get_balance", "read_only"),
        (2, "list_transactions", "read_only"),
        (3, "send_email", "side_effect"),
    ]
    assert all(s["status"] == "ok" for s in steps)
    # The journal keeps what the response leaves out.
    assert json.loads(steps[1]["payload_json"]) == {
        "from": "2024-01-01",
        "to": "2024-12-31",
    }
    assert json.loads(steps[2]["result_json"]) == {"sent": True}


def test_both_attempts_are_journaled_separately(
    home: tuple[RunlacePaths, Connection]
) -> None:
    _, conn = home
    store(home)
    refused = execute(home)
    completed = execute(home, confirm=True)

    assert refused["run_id"] != completed["run_id"]
    rows = list(conn.execute("SELECT id, status, confirmed FROM runs ORDER BY rowid"))
    assert [(r["status"], r["confirmed"]) for r in rows] == [("failed", 0), ("completed", 1)]


def test_a_read_only_workflow_needs_no_confirmation(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """D6 gates side effects, not runs."""
    store(home, READ_ONLY, name="balance")
    result = execute(home, workflow="balance")
    assert result["ok"] is True
    assert result["output"] == {"balance": {"balance": 1234.5}, "to": "a@b.c"}


def test_a_policy_edit_gates_a_workflow_that_already_exists(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """An override that only protects workflows written after it protects little.

    `get_balance` is annotated read_only, so this workflow was stored ungated.
    Marking it a side effect in `policy.yaml` has to reach it without recreating
    it -- that is the whole point of noticing a tool is dangerous.
    """
    paths, _ = home
    store(home, READ_ONLY, name="balance")
    assert execute(home, workflow="balance")["ok"] is True

    paths.policy.write_text(
        "risk:\n  pennylane:\n    get_balance: side_effect\n", encoding="utf-8"
    )

    refused = execute(home, workflow="balance")
    assert refused["code"] == CODE_NEEDS_CONFIRMATION
    assert refused["side_effects"] == [{"connector": "pennylane", "tool": "get_balance"}]
    assert execute(home, workflow="balance", confirm=True)["ok"] is True


def test_a_policy_edit_cannot_ungate_a_workflow_that_already_exists(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """Overrides tighten in place and loosen only through a new version.

    Relaxing is the direction where being wrong costs something, so it is the
    direction that has to go through create_workflow again.
    """
    paths, _ = home
    store(home)
    paths.policy.write_text(
        "risk:\n  gmail:\n    send_email: read_only\n", encoding="utf-8"
    )
    assert execute(home)["code"] == CODE_NEEDS_CONFIRMATION


def test_a_broken_policy_file_does_not_stop_a_run(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """It must fail toward the gate, not toward the exception."""
    paths, _ = home
    store(home, READ_ONLY, name="balance")
    paths.policy.write_text("risk:\n  - [unclosed\n", encoding="utf-8")
    assert execute(home, workflow="balance")["ok"] is True


def test_the_reported_steps_leave_the_data_in_the_journal(
    home: tuple[RunlacePaths, Connection]
) -> None:
    store(home)
    result = execute(home, confirm=True)
    assert all(
        set(s) == {"seq", "connector", "tool", "risk", "status", "duration_ms", "error"}
        for s in result["steps"]
    )


def test_get_step_reads_back_what_the_run_did_not_report(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """The other half of the rule above: withheld, not lost.

    The workflow returns a count; the fifty rows it counted stay in the journal,
    and an agent that needs to see their shape asks for them one step at a time.
    """
    _, conn = home
    store(home)
    rows = [{"id": str(i), "amount": float(i)} for i in range(50)]
    result = execute(
        home,
        confirm=True,
        call_tool=Recorder({"list_transactions": {"transactions": rows}}),
    )

    assert result["output"]["count"] == 50
    assert "amount" not in json.dumps(result["steps"])

    step = get_step(conn, result["run_id"], 2)
    assert (step["connector"], step["tool"]) == ("pennylane", "list_transactions")
    assert step["result"]["transactions"][0] == {"id": "0", "amount": 0.0}
    assert len(step["result"]["transactions"]) == MAX_ITEMS
    assert f"result.transactions: kept {MAX_ITEMS} of 50 items" in step["trimmed"]


# -- gate 1: inputs --------------------------------------------------------


def test_inputs_that_do_not_match_the_schema_are_refused_per_field(
    home: tuple[RunlacePaths, Connection]
) -> None:
    _, conn = home
    store(home)
    recorder = Recorder()
    result = execute(home, inputs={}, confirm=True, call_tool=recorder)

    assert result["code"] == CODE_INVALID_INPUTS
    assert [e["field"] for e in result["errors"]] == ["to"]
    assert recorder.calls == []

    row, steps = journal(conn, result["run_id"])
    assert row["status"] == "failed" and steps == []


def test_the_inputs_gate_comes_before_the_confirm_gate(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """Asking a human to confirm a run that cannot start would waste their time."""
    store(home)
    result = execute(home, inputs={})
    assert result["code"] == CODE_INVALID_INPUTS


def test_declared_defaults_are_filled_in_and_journaled(
    home: tuple[RunlacePaths, Connection]
) -> None:
    paths, conn = home
    schema = {
        "type": "object",
        "properties": {"to": {"type": "string", "default": "ops@example.com"}},
        "required": ["to"],
    }
    result = create_workflow(
        conn, paths, name="balance", description="d", code=READ_ONLY,
        inputs_schema=schema,
    )
    assert result.ok, [str(e) for e in result.errors]

    run = execute(home, workflow="balance", inputs={})
    assert run["ok"] is True
    assert run["output"]["to"] == "ops@example.com"
    row, _ = journal(conn, run["run_id"])
    assert json.loads(row["inputs_json"]) == {"to": "ops@example.com"}


# -- gate 2: drift ---------------------------------------------------------


def test_a_workflow_whose_tools_changed_shape_refuses_to_run(
    home: tuple[RunlacePaths, Connection]
) -> None:
    _, conn = home
    store(home)
    conn.execute(
        "UPDATE tools SET schema_hash = 'sha256:changed' WHERE name = 'get_balance'"
    )
    conn.commit()

    recorder = Recorder()
    result = execute(home, confirm=True, call_tool=recorder)
    assert result["code"] == CODE_SCHEMA_DRIFT
    assert "get_balance changed its schema" in result["error"]
    assert "runlace sync" in result["hint"]
    assert recorder.calls == []

    row, steps = journal(conn, result["run_id"])
    assert row["status"] == "failed" and steps == []


def test_a_tool_that_disappeared_refuses_too(
    home: tuple[RunlacePaths, Connection]
) -> None:
    _, conn = home
    store(home)
    conn.execute("DELETE FROM tools WHERE name = 'send_email'")
    conn.commit()

    result = execute(home, confirm=True)
    assert result["code"] == CODE_SCHEMA_DRIFT
    assert "no longer exists" in result["error"]


def test_the_drift_gate_comes_before_the_confirm_gate(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """A workflow that cannot run is not worth confirming."""
    _, conn = home
    store(home)
    conn.execute("UPDATE tools SET schema_hash = 'sha256:changed'")
    conn.commit()
    assert execute(home)["code"] == CODE_SCHEMA_DRIFT


# -- gate 4: the run itself ------------------------------------------------


def test_a_workflow_that_raises_is_journaled_with_the_steps_it_got_through(
    home: tuple[RunlacePaths, Connection]
) -> None:
    _, conn = home
    store(
        home,
        "from runlace_types import Ctx\n\n\n"
        "def run(ctx: Ctx) -> dict[str, object]:\n"
        '    ctx.gmail.send_email(to=ctx.inputs["to"], subject="s", body="b")\n'
        '    return {"x": 1 / 0}\n',
    )
    result = execute(home, confirm=True)

    assert result["code"] == CODE_WORKFLOW_FAILED
    assert result["detail"]["type"] == "ZeroDivisionError"
    assert result["detail"]["line"] == 6

    row, steps = journal(conn, result["run_id"])
    assert row["status"] == "failed"
    assert "division by zero" in row["error"]
    # The email went out before the crash. The journal is where you find that out.
    assert [s["tool"] for s in steps] == ["send_email"]


def test_a_failing_tool_is_journaled_as_an_errored_step(
    home: tuple[RunlacePaths, Connection]
) -> None:
    _, conn = home
    store(home, READ_ONLY, name="balance")
    result = execute(
        home,
        workflow="balance",
        call_tool=Recorder({"get_balance": ToolFailed("the server said no")}),
    )
    assert result["ok"] is False
    _, steps = journal(conn, result["run_id"])
    assert steps[0]["status"] == "error"
    assert steps[0]["error"] == "the server said no"


def test_a_connector_that_is_no_longer_configured_refuses_the_run(
    home: tuple[RunlacePaths, Connection]
) -> None:
    paths, conn = home
    store(home, READ_ONLY, name="balance")
    paths.config.write_text('{"connectors": {}}', encoding="utf-8")

    result = execute(home, workflow="balance")
    assert result["code"] == CODE_UNKNOWN_CONNECTOR
    assert "pennylane" in result["error"]
    row, _ = journal(conn, result["run_id"])
    assert row["status"] == "failed"


# -- gate 5: the output ----------------------------------------------------


OUTPUTS = {
    "type": "object",
    "properties": {"to": {"type": "string"}, "amount": {"type": "number"}},
    "required": ["to", "amount"],
}

ANNOTATED = (
    "from runlace_types import Ctx, Output\n\n\n"
    "def run(ctx: Ctx) -> Output:\n"
    '    txs = ctx.pennylane.list_transactions(from_="2024-01-01", to="2024-12-31")\n'
    '    return {"to": ctx.inputs["to"], "amount": txs["transactions"][0]["amount"]}\n'
)


def test_an_output_that_matches_its_schema_completes(
    home: tuple[RunlacePaths, Connection]
) -> None:
    store(home, ANNOTATED, name="statement", outputs_schema=OUTPUTS)
    result = execute(home, workflow="statement")
    assert result["ok"] is True
    assert result["output"] == {"to": "a@b.c", "amount": 1.0}


def test_an_output_that_does_not_match_fails_the_run_but_keeps_the_value(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """When a server's declared output schema turns out to be a lie.

    pyright had no way to know: it trusted the schema at create time. D7's
    runtime check is what catches it, and the value is still reported -- the
    side effects already happened, and hiding what came back helps nobody.
    """
    _, conn = home
    store(home, ANNOTATED, name="statement", outputs_schema=OUTPUTS)
    result = execute(
        home,
        workflow="statement",
        call_tool=Recorder(
            {"list_transactions": {"transactions": [{"id": "a", "amount": "lots"}]}}
        ),
    )

    assert result["code"] == CODE_INVALID_OUTPUT
    assert [e["field"] for e in result["errors"]] == ["amount"]
    assert result["output"] == {"to": "a@b.c", "amount": "lots"}

    row, _ = journal(conn, result["run_id"])
    assert row["status"] == "failed"
    assert json.loads(row["output_json"]) == result["output"]


# -- addressing a workflow -------------------------------------------------


def test_an_unknown_workflow_leaves_no_run_row(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """There is no version to attach a run to, so there is nothing to journal."""
    _, conn = home
    result = execute(home, workflow="nope")
    assert result["code"] == CODE_UNKNOWN_WORKFLOW
    assert result["run_id"] is None
    assert conn.execute("SELECT count(*) AS n FROM runs").fetchone()["n"] == 0


def test_a_run_can_name_an_older_version(
    home: tuple[RunlacePaths, Connection]
) -> None:
    paths, conn = home
    first = create_workflow(
        conn, paths, name="balance", description="v1", code=READ_ONLY,
        inputs_schema=INPUTS,
    )
    second = create_workflow(
        conn,
        paths,
        name="balance",
        description="v2",
        code=READ_ONLY.replace('"to": ctx.inputs["to"]', '"to": "pinned"'),
        inputs_schema=INPUTS,
    )
    assert first.ok and second.ok and first.version != second.version

    latest = execute(home, workflow="balance")
    assert latest["output"]["to"] == "pinned"
    assert latest["version"] == second.version

    old = execute(home, workflow="balance", version=first.version)
    assert old["output"]["to"] == "a@b.c"
    assert old["version"] == first.version


def test_a_version_that_does_not_exist_says_so(
    home: tuple[RunlacePaths, Connection]
) -> None:
    store(home, READ_ONLY, name="balance")
    result = execute(home, workflow="balance", version="sha256:nope")
    assert result["code"] == CODE_UNKNOWN_WORKFLOW
    assert result["run_id"] is None


@pytest.mark.parametrize("key", ["report", "by-id"])
def test_a_workflow_can_be_addressed_by_name_or_by_id(
    home: tuple[RunlacePaths, Connection], key: str
) -> None:
    paths, conn = home
    created = create_workflow(
        conn, paths, name="report", description="d", code=READ_ONLY,
        inputs_schema=INPUTS,
    )
    assert created.ok and created.workflow_id is not None
    workflow = created.workflow_id if key == "by-id" else "report"
    assert execute(home, workflow=workflow)["ok"] is True


# -- the dry run -----------------------------------------------------------


def test_a_dry_run_reads_for_real_and_never_lets_a_tool_act(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """The bargain the whole feature rests on, in one assertion each way."""
    store(home)
    recorder = Recorder()
    result = execute(home, dry_run=True, call_tool=recorder)

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["simulated"] == [{"connector": "gmail", "tool": "send_email"}]
    # The reads really happened; the send never reached the wire.
    assert [t for _, t, _ in recorder.calls] == ["get_balance", "list_transactions"]
    assert result["output"] == {"balance": {"balance": 1234.5}, "count": 1}


def test_a_dry_run_needs_no_confirmation(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """D6 gates acting on the world. A dry run does not act on the world."""
    store(home)
    assert execute(home)["code"] == CODE_NEEDS_CONFIRMATION
    assert execute(home, dry_run=True)["ok"] is True


def test_a_stood_in_value_has_the_shape_the_server_declared(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """The point of standing in from `outputSchema` rather than returning None.

    `list_transactions` is read-only in the fixture, so `policy.yaml` is used to
    make it act -- and then the line after it, `len(txs["transactions"])`, has to
    keep working on a value nobody fetched.
    """
    paths, _ = home
    store(home)
    paths.policy.write_text(
        "risk:\n  pennylane:\n    list_transactions: side_effect\n", encoding="utf-8"
    )

    recorder = Recorder()
    result = execute(home, dry_run=True, call_tool=recorder)

    assert result["ok"] is True
    assert [t for _, t, _ in recorder.calls] == ["get_balance"]
    # One element, so the loop body runs once and the length is countable.
    assert result["output"]["count"] == 1


def test_a_dry_run_is_journaled_and_marked_as_one(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """It has to be in the audit log, and it has to be impossible to mistake."""
    _, conn = home
    store(home)
    result = execute(home, dry_run=True)

    row, steps = journal(conn, result["run_id"])
    assert row["status"] == "completed"
    assert row["dry_run"] == 1
    # The stood-in call is a step like any other: it is what the workflow did.
    assert [s["tool"] for s in steps] == [
        "get_balance",
        "list_transactions",
        "send_email",
    ]


def test_a_dry_run_does_not_count_as_the_last_run(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """"When did this last run" is a question about the world."""
    _, conn = home
    store(home)
    execute(home, dry_run=True)
    assert list_workflows(conn)[0]["last_run"] is None

    execute(home, confirm=True)
    assert list_workflows(conn)[0]["last_run"]["status"] == "completed"


def test_a_dry_run_does_not_need_the_connector_it_never_calls(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """You can check a workflow before the server that would act is reachable."""
    paths, _ = home
    store(home)
    paths.config.write_text(
        json.dumps(
            {"connectors": {"pennylane": {"command": "pennylane", "args": []}}}
        ),
        encoding="utf-8",
    )

    assert execute(home, confirm=True)["code"] == CODE_UNKNOWN_CONNECTOR
    assert execute(home, dry_run=True)["ok"] is True


def test_a_dry_run_still_validates_its_inputs(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """Only the confirm gate is lifted; the rest of the order is the same."""
    store(home)
    assert execute(home, inputs={}, dry_run=True)["code"] == CODE_INVALID_INPUTS


def test_a_dry_run_that_returns_the_wrong_shape_says_nothing_acted(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """The same defect reads differently when the side effects have not happened."""
    store(home, ANNOTATED, name="statement", outputs_schema=OUTPUTS)
    real = execute(
        home,
        workflow="statement",
        call_tool=Recorder(
            {"list_transactions": {"transactions": [{"id": "a", "amount": "lots"}]}}
        ),
    )
    dry = execute(
        home,
        workflow="statement",
        dry_run=True,
        call_tool=Recorder(
            {"list_transactions": {"transactions": [{"id": "a", "amount": "lots"}]}}
        ),
    )

    assert real["code"] == dry["code"] == CODE_INVALID_OUTPUT
    assert "side effects happened" in real["hint"]
    assert "Nothing acted" in dry["hint"]
