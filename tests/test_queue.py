"""The run state machine: claiming, approving, refusing, giving up.

Every test here goes through the database rather than a fake, because the thing
being tested *is* the database: a queue whose durability is the point deserves
tests that would notice if it stopped being durable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from runlace.db import (
    Connection,
    claim_queued_run,
    connect,
    count_runs_with_status,
    find_run,
    insert_run,
    insert_workflow,
    insert_workflow_version,
    now_iso,
)
from runlace.paths import RunlacePaths
from runlace.queue import (
    AWAITING_APPROVAL,
    CODE_NOT_PARKED,
    CODE_UNKNOWN_RUN,
    COMPLETED,
    EXPIRED,
    QUEUED,
    REJECTED,
    RUNNING,
    approve,
    expire,
    reject,
)

VERSION_ID = "wv_1"


@pytest.fixture
def conn(paths: RunlacePaths) -> Connection:
    connection = connect(paths.db)
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
    connection.commit()
    return connection


def a_run(
    conn: Connection,
    run_id: str,
    *,
    status: str,
    queued_at: str | None = None,
    inputs: dict[str, object] | None = None,
) -> str:
    insert_run(
        conn,
        run_id=run_id,
        workflow_version_id=VERSION_ID,
        inputs=inputs or {},
        confirmed=False,
        status=status,
        queued_at=queued_at,
    )
    conn.commit()
    return run_id


def test_claiming_takes_the_oldest_first(conn: Connection) -> None:
    """A queue that is not FIFO is a lottery, and a slow run would starve."""
    a_run(conn, "run_b", status=QUEUED, queued_at="2026-01-01T10:00:00+00:00")
    a_run(conn, "run_a", status=QUEUED, queued_at="2026-01-01T09:00:00+00:00")

    first = claim_queued_run(conn)
    second = claim_queued_run(conn)

    assert first is not None and first["id"] == "run_a"
    assert second is not None and second["id"] == "run_b"
    assert claim_queued_run(conn) is None


def test_claiming_marks_it_running_so_nobody_else_takes_it(conn: Connection) -> None:
    a_run(conn, "run_1", status=QUEUED, queued_at=now_iso())

    claimed = claim_queued_run(conn)

    assert claimed is not None
    assert str(claimed["status"]) == RUNNING
    assert count_runs_with_status(conn, QUEUED) == 0


def test_a_parked_run_is_never_claimed(conn: Connection) -> None:
    """The whole point of parking: no worker touches it until a human answers."""
    a_run(conn, "run_parked", status=AWAITING_APPROVAL)

    assert claim_queued_run(conn) is None


def test_approving_puts_it_in_line_without_running_it(conn: Connection) -> None:
    a_run(conn, "run_parked", status=AWAITING_APPROVAL, inputs={"to": "a@b.c"})

    assert approve(conn, "run_parked") == {
        "ok": True,
        "run_id": "run_parked",
        "status": QUEUED,
    }

    row = find_run(conn, "run_parked")
    assert row is not None
    assert str(row["status"]) == QUEUED
    assert row["approved_at"] is not None
    # The inputs a human approved are the inputs that will run. Approval cannot
    # be a second chance to change them.
    assert str(row["inputs_json"]) == '{"to":"a@b.c"}'


def test_refusing_ends_the_run_and_keeps_the_reason(conn: Connection) -> None:
    a_run(conn, "run_parked", status=AWAITING_APPROVAL)

    result = reject(conn, "run_parked", "wrong recipient")

    assert result["status"] == REJECTED
    row = find_run(conn, "run_parked")
    assert row is not None
    assert str(row["approval_note"]) == "wrong recipient"
    assert row["finished_at"] is not None


def test_approving_a_run_that_already_ran_says_so_instead_of_running_it_again(
    conn: Connection,
) -> None:
    """The late click. It must not resurrect a finished run."""
    a_run(conn, "run_done", status=COMPLETED)

    result = approve(conn, "run_done")

    assert result["ok"] is False
    assert result["code"] == CODE_NOT_PARKED
    assert "already over" in result["hint"]
    row = find_run(conn, "run_done")
    assert row is not None and str(row["status"]) == COMPLETED


def test_approving_twice_is_not_a_second_run(conn: Connection) -> None:
    a_run(conn, "run_parked", status=AWAITING_APPROVAL)
    approve(conn, "run_parked")

    second = approve(conn, "run_parked")

    assert second["code"] == CODE_NOT_PARKED
    assert "waiting for a worker" in second["hint"]


def test_approving_a_run_that_does_not_exist(conn: Connection) -> None:
    assert approve(conn, "run_nope")["code"] == CODE_UNKNOWN_RUN


def test_a_parked_run_nobody_answered_is_given_up_on(conn: Connection) -> None:
    stale = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat(
        timespec="seconds"
    )
    a_run(conn, "run_stale", status=AWAITING_APPROVAL)
    conn.execute(
        "UPDATE runs SET started_at = ? WHERE id = ?", (stale, "run_stale")
    )
    a_run(conn, "run_fresh", status=AWAITING_APPROVAL)
    conn.commit()

    assert expire(conn, ttl_seconds=24 * 60 * 60) == ["run_stale"]

    stale_row = find_run(conn, "run_stale")
    fresh_row = find_run(conn, "run_fresh")
    assert stale_row is not None and str(stale_row["status"]) == EXPIRED
    assert fresh_row is not None and str(fresh_row["status"]) == AWAITING_APPROVAL


def test_giving_up_never_touches_a_run_that_is_not_parked(conn: Connection) -> None:
    """Expiry walks the whole table; a queued run must not be swept up with it."""
    a_run(conn, "run_queued", status=QUEUED, queued_at="2020-01-01T00:00:00+00:00")
    conn.execute(
        "UPDATE runs SET started_at = ? WHERE id = ?",
        ("2020-01-01T00:00:00+00:00", "run_queued"),
    )
    conn.commit()

    assert expire(conn, ttl_seconds=1) == []

    row = find_run(conn, "run_queued")
    assert row is not None and str(row["status"]) == QUEUED
