"""The daemon that drains the queue.

These tests run a real worker against a real database and a real subprocess.
The workflows are deliberately tool-free: what is under test is the loop around
execution -- claim it, rebuild what was admitted, finish it, write it down --
and a live MCP server in the middle would only make the failures harder to read.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import pytest

from runlace.db import (
    Connection,
    connect,
    find_run,
    insert_run,
    now_iso,
    queue_lock_holder,
)
from runlace.paths import RunlacePaths
from runlace.queue import AWAITING_APPROVAL, COMPLETED, QUEUED
from runlace.worker import Worker, has_live_worker
from runlace.workflows import create_workflow, get_workflow

INPUTS = {
    "type": "object",
    "properties": {"to": {"type": "string"}},
    "required": ["to"],
}

# No tool calls at all: this exercises the worker, not the MCP bridge.
NO_TOOLS = (
    "from runlace_types import Ctx\n\n\n"
    "def run(ctx: Ctx) -> dict[str, object]:\n"
    '    return {"greeted": ctx.inputs["to"]}\n'
)


@pytest.fixture
def stored(home: tuple[RunlacePaths, Connection]) -> tuple[RunlacePaths, Connection, str]:
    """A compiled workflow, and the id of the version a run would point at."""
    paths, conn = home
    result = create_workflow(
        conn,
        paths,
        name="greet",
        description="Say hello.",
        code=NO_TOOLS,
        inputs_schema=INPUTS,
    )
    assert result.ok, [str(e) for e in result.errors]
    record = get_workflow(conn, "greet")
    assert record is not None
    return paths, conn, str(record["version_id"])


def a_run(
    conn: Connection,
    version_id: str,
    run_id: str,
    *,
    status: str,
    inputs: dict[str, Any] | None = None,
) -> str:
    insert_run(
        conn,
        run_id=run_id,
        workflow_version_id=version_id,
        inputs=inputs if inputs is not None else {"to": "a@b.c"},
        confirmed=True,
        status=status,
        queued_at=now_iso() if status == QUEUED else None,
    )
    conn.commit()
    return run_id


async def drain_until(
    paths: RunlacePaths,
    done: Callable[[], bool],
    *,
    timeout: float = 20.0,
    **kwargs: Any,
) -> None:
    """Run a worker until ``done`` or the clock runs out, then stop it cleanly."""
    stop = asyncio.Event()
    task = asyncio.create_task(Worker(paths, **kwargs).drain(stop))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not done():
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=timeout)


def status_of(conn: Connection, run_id: str) -> str:
    row = find_run(conn, run_id)
    assert row is not None
    return str(row["status"])


def test_a_queued_run_is_picked_up_and_completed(
    stored: tuple[RunlacePaths, Connection, str]
) -> None:
    paths, conn, version_id = stored
    a_run(conn, version_id, "run_q", status=QUEUED)

    asyncio.run(drain_until(paths, lambda: status_of(conn, "run_q") == COMPLETED))

    row = find_run(conn, "run_q")
    assert row is not None
    assert str(row["status"]) == COMPLETED
    assert str(row["output_json"]) == '{"greeted":"a@b.c"}'
    assert row["finished_at"] is not None


def test_the_inputs_that_run_are_the_inputs_that_were_admitted(
    stored: tuple[RunlacePaths, Connection, str]
) -> None:
    """A queued run carries its own inputs. Nothing re-derives them later."""
    paths, conn, version_id = stored
    a_run(conn, version_id, "run_q", status=QUEUED, inputs={"to": "someone@else.org"})

    asyncio.run(drain_until(paths, lambda: status_of(conn, "run_q") == COMPLETED))

    row = find_run(conn, "run_q")
    assert row is not None
    assert str(row["output_json"]) == '{"greeted":"someone@else.org"}'


def test_a_parked_run_is_left_where_it_is(
    stored: tuple[RunlacePaths, Connection, str]
) -> None:
    """The worker must never be the thing that decides a human said yes."""
    paths, conn, version_id = stored
    a_run(conn, version_id, "run_parked", status=AWAITING_APPROVAL)
    a_run(conn, version_id, "run_q", status=QUEUED)

    asyncio.run(drain_until(paths, lambda: status_of(conn, "run_q") == COMPLETED))

    assert status_of(conn, "run_parked") == AWAITING_APPROVAL


def test_several_queued_runs_all_get_done(
    stored: tuple[RunlacePaths, Connection, str]
) -> None:
    paths, conn, version_id = stored
    ids = [a_run(conn, version_id, f"run_{i}", status=QUEUED) for i in range(5)]

    asyncio.run(
        drain_until(
            paths,
            lambda: all(status_of(conn, i) == COMPLETED for i in ids),
            concurrency=2,
        )
    )

    assert [status_of(conn, i) for i in ids] == [COMPLETED] * 5


def test_a_second_worker_on_the_same_home_does_not_drain(
    stored: tuple[RunlacePaths, Connection, str]
) -> None:
    """Two daemons on one ~/.runlace must not both run the same side effect."""
    paths, conn, _ = stored

    async def scenario() -> bool:
        stop = asyncio.Event()
        first = Worker(paths)
        task = asyncio.create_task(first.drain(stop))
        # Wait for the first to actually own the lock before asking the second.
        while queue_lock_holder(conn) is None:
            await asyncio.sleep(0.01)

        second_stop = asyncio.Event()
        second = Worker(paths)
        second.owner_pid = first.owner_pid + 1
        await second.drain(second_stop)  # returns at once; it did not get the lock

        holder = queue_lock_holder(conn)
        stop.set()
        await task
        return holder is not None and int(holder["owner_pid"]) == first.owner_pid

    assert asyncio.run(scenario())


def test_a_lock_nobody_renewed_is_taken_over(
    stored: tuple[RunlacePaths, Connection, str]
) -> None:
    """A daemon that was killed must not lock the queue until someone notices."""
    paths, conn, version_id = stored
    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(
        timespec="seconds"
    )
    conn.execute(
        "INSERT INTO queue_lock(id, owner_pid, hostname, heartbeat) VALUES(?, ?, ?, ?)",
        ("drainer", 999999, "a-laptop-that-slept", stale),
    )
    conn.commit()
    a_run(conn, version_id, "run_q", status=QUEUED)

    asyncio.run(drain_until(paths, lambda: status_of(conn, "run_q") == COMPLETED))

    assert status_of(conn, "run_q") == COMPLETED


def test_the_lock_is_let_go_on_a_clean_stop(
    stored: tuple[RunlacePaths, Connection, str]
) -> None:
    """So the next daemon starts now rather than waiting out the timeout."""
    paths, conn, _ = stored

    asyncio.run(drain_until(paths, lambda: queue_lock_holder(conn) is not None))

    assert queue_lock_holder(conn) is None


def test_nobody_draining_is_a_question_you_can_ask(
    stored: tuple[RunlacePaths, Connection, str]
) -> None:
    """`run_workflow` needs this answer to know whether it may hand a run over."""
    paths, conn, _ = stored
    assert has_live_worker(conn) is False

    async def scenario() -> bool:
        stop = asyncio.Event()
        task = asyncio.create_task(Worker(paths).drain(stop))
        while queue_lock_holder(conn) is None:
            await asyncio.sleep(0.01)
        live = has_live_worker(connect(paths.db))
        stop.set()
        await task
        return live

    assert asyncio.run(scenario()) is True
    assert has_live_worker(conn) is False


def test_a_run_that_started_is_allowed_to_finish_after_stop(
    stored: tuple[RunlacePaths, Connection, str]
) -> None:
    """Killing a workflow halfway through its side effects to shut down half a
    second sooner is the worst trade in the system."""
    paths, conn, version_id = stored
    a_run(conn, version_id, "run_q", status=QUEUED)

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(Worker(paths).drain(stop))
        while status_of(conn, "run_q") == QUEUED:
            await asyncio.sleep(0.01)
        stop.set()  # mid-flight
        await asyncio.wait_for(task, timeout=30.0)

    asyncio.run(scenario())

    assert status_of(conn, "run_q") == COMPLETED
