"""`get_step`: reading one tool call back out of the journal.

The trimming is the part worth testing. It exists so an agent can see the shape
of what a tool returned without pulling the data itself back into its context,
and a trim that changed the shape would defeat both halves of that.
"""

from __future__ import annotations

from typing import Any

import pytest

from runlace.db import (
    Connection,
    connect,
    insert_run,
    insert_step,
    insert_workflow,
    insert_workflow_version,
)
from runlace.journal import MAX_ITEMS, MAX_STRING, get_step
from runlace.paths import RunlacePaths

RUN_ID = "run_abc123"
VERSION_ID = "wv_1"


def a_workflow_version(connection: Connection) -> None:
    """The rows a run points at. `get_step` never reads them, sqlite insists."""
    insert_workflow(connection, workflow_id="wf_1", name="report", description=None)
    insert_workflow_version(
        connection,
        version_id=VERSION_ID,
        workflow_id="wf_1",
        version_hash="deadbeef",
        file_path="/nowhere/report.py",
        inputs_schema=None,
        outputs_schema=None,
        tools_used=[],
    )


@pytest.fixture
def conn(paths: RunlacePaths) -> Connection:
    """A run with one step, whose result is bigger than anyone wants to read."""
    connection = connect(paths.db)
    a_workflow_version(connection)
    insert_run(
        connection,
        run_id=RUN_ID,
        workflow_version_id=VERSION_ID,
        inputs={},
        confirmed=False,
    )
    insert_step(
        connection,
        step_id="st_1",
        run_id=RUN_ID,
        seq=1,
        connector="github",
        tool="search_code",
        risk="read_only",
        payload={"query": "def run(", "fields": ["name", "path", "repository"]},
        result={
            "total_count": 50,
            "items": [{"name": f"f{i}.py", "body": "x" * 1000} for i in range(50)],
        },
        status="ok",
        duration_ms=42,
        error=None,
    )
    connection.commit()
    return connection


def test_a_long_list_is_cut_but_stays_a_list_of_objects(conn: Connection) -> None:
    """A "... 47 more" marker inside the list would misreport the shape."""
    items: list[Any] = get_step(conn, RUN_ID, 1)["result"]["items"]

    assert len(items) == MAX_ITEMS
    assert all(isinstance(item, dict) for item in items)
    assert items[0]["name"] == "f0.py"


def test_a_long_string_is_cut(conn: Connection) -> None:
    body: str = get_step(conn, RUN_ID, 1)["result"]["items"][0]["body"]

    assert len(body) == MAX_STRING


def test_what_was_dropped_is_said_out_of_band(conn: Connection) -> None:
    trimmed: list[str] = get_step(conn, RUN_ID, 1)["trimmed"]

    assert f"result.items: kept {MAX_ITEMS} of 50 items" in trimmed
    assert any("result.items[0].body: kept 300 of 1000 characters" == n for n in trimmed)


def test_the_real_size_is_reported_even_though_it_is_not_returned(
    conn: Connection,
) -> None:
    """The number that makes the case for aggregating in the workflow."""
    step = get_step(conn, RUN_ID, 1)

    assert step["result_chars"] > 50_000


def test_the_arguments_the_workflow_sent_come_back_verbatim(conn: Connection) -> None:
    """Found live: a `fields` list trimmed to two of three hid the answer.

    The arguments are the workflow's own. An agent reading a step is asking
    what it sent, and a trimmed answer to that question is a wrong one.
    """
    step = get_step(conn, RUN_ID, 1)

    assert step["payload"] == {
        "query": "def run(",
        "fields": ["name", "path", "repository"],
    }
    assert not any(n.startswith("payload") for n in step["trimmed"])
    assert (step["connector"], step["tool"], step["status"]) == (
        "github",
        "search_code",
        "ok",
    )


def test_a_short_result_is_returned_whole(paths: RunlacePaths) -> None:
    connection = connect(paths.db)
    a_workflow_version(connection)
    insert_run(
        connection,
        run_id="run_small",
        workflow_version_id=VERSION_ID,
        inputs={},
        confirmed=False,
    )
    insert_step(
        connection,
        step_id="st_2",
        run_id="run_small",
        seq=1,
        connector="everything",
        tool="echo",
        risk="read_only",
        payload={"message": "hi"},
        result={"echoed": "hi"},
        status="ok",
        duration_ms=1,
        error=None,
    )
    connection.commit()

    step = get_step(connection, "run_small", 1)

    assert step["result"] == {"echoed": "hi"}
    assert step["trimmed"] == []


def test_arguments_big_enough_to_be_the_problem_are_trimmed_after_all(
    paths: RunlacePaths,
) -> None:
    """The exception to the rule above: a bulk create is not a debugging aid."""
    connection = connect(paths.db)
    a_workflow_version(connection)
    insert_run(
        connection,
        run_id="run_bulk",
        workflow_version_id=VERSION_ID,
        inputs={},
        confirmed=False,
    )
    insert_step(
        connection,
        step_id="st_3",
        run_id="run_bulk",
        seq=1,
        connector="files",
        tool="write",
        risk="side_effect",
        payload={"lines": [f"line {i}" for i in range(500)]},
        result={"written": True},
        status="ok",
        duration_ms=5,
        error=None,
    )
    connection.commit()

    step = get_step(connection, "run_bulk", 1)

    assert len(step["payload"]["lines"]) == MAX_ITEMS
    assert f"payload.lines: kept {MAX_ITEMS} of 500 items" in step["trimmed"]


def test_an_unknown_run_is_not_confused_with_an_unknown_step(conn: Connection) -> None:
    assert get_step(conn, "run_nope", 1)["code"] == "unknown-run"

    missing = get_step(conn, RUN_ID, 7)
    assert missing["code"] == "unknown-step"
    assert missing["steps"] == [1]
    assert "1 to 1" in missing["hint"]
