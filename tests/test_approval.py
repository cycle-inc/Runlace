"""Parking a run until a human answers, and what happens when they do.

v1 refused a side-effecting run and told the agent to ask the human sitting in
the same conversation. There is no such human in the shape this is built for --
an end user, a chatbot a developer built, and Runlace inside that developer's
backend -- so the run is set aside instead, and the developer's product asks
somebody we will never meet.

These tests run the whole way through: a real worker, a real MCP server over
stdio, and a tool that really writes a file. Nothing here is mocked, because
the thing being proved is that after approval the side effect actually happens.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

import pytest

from runlace.config import Connector, read_config, write_config
from runlace.db import Connection, find_run
from runlace.journal import get_run
from runlace.paths import RunlacePaths
from runlace.queue import (
    APPROVAL_ALLOW,
    AWAITING_APPROVAL,
    COMPLETED,
    QUEUED,
    REJECTED,
    approve,
    has_live_worker,
    pending,
    reject,
)
from runlace.runs import run_workflow
from runlace.worker import Worker
from runlace.workflows import create_workflow

SERVER = Path(__file__).parent / "fixtures" / "stdio_server.py"

INPUTS = {
    "type": "object",
    "properties": {"to": {"type": "string"}},
    "required": ["to"],
}

MAILER = (
    "from runlace_types import Ctx\n\n\n"
    "def run(ctx: Ctx) -> dict[str, object]:\n"
    '    ctx.gmail.send_email(to=ctx.inputs["to"], subject="hi", body="hello")\n'
    '    return {"sent": ctx.inputs["to"]}\n'
)


@pytest.fixture
def mailer(home: tuple[RunlacePaths, Connection]) -> tuple[RunlacePaths, Connection, Path]:
    """A workflow that sends an email, and a `gmail` that really receives it.

    The connector rows the `home` fixture wrote stay exactly as they are -- the
    schemas the workflow was compiled against are what the drift gate reads. All
    that changes is where `gmail` actually points: at a server that can answer.
    """
    paths, conn = home
    outbox = paths.home / "outbox.txt"
    write_config(
        paths.config,
        [
            Connector(
                name="gmail",
                attr="gmail",
                transport="stdio",
                command=sys.executable,
                args=[str(SERVER), str(outbox)],
            )
            if c.name == "gmail"
            else c
            for c in read_config(paths.config)
        ],
    )
    result = create_workflow(
        conn,
        paths,
        name="mailer",
        description="Send one email.",
        code=MAILER,
        inputs_schema=INPUTS,
    )
    assert result.ok, [str(e) for e in result.errors]
    return paths, conn, outbox


async def with_a_worker(
    paths: RunlacePaths, conn: Connection, body: Callable[[], Awaitable[Any]]
) -> Any:
    stop = asyncio.Event()
    task = asyncio.create_task(Worker(paths).drain(stop))
    try:
        while not has_live_worker(conn):
            await asyncio.sleep(0.01)
        return await body()
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=60.0)


async def until_finished(conn: Connection, run_id: str) -> dict[str, Any]:
    for _ in range(3000):
        run = get_run(conn, run_id)
        if run["finished"]:
            return run
        await asyncio.sleep(0.02)
    raise AssertionError(f"run {run_id} never finished")


def test_a_side_effect_run_parks_instead_of_being_refused(
    mailer: tuple[RunlacePaths, Connection, Path]
) -> None:
    paths, conn, outbox = mailer

    async def scenario() -> dict[str, Any]:
        return await with_a_worker(
            paths,
            conn,
            lambda: run_workflow(conn, paths, workflow="mailer", inputs={"to": "a@b.c"}),
        )

    result = asyncio.run(scenario())

    assert result["code"] == "awaiting-approval"
    assert result["status"] == AWAITING_APPROVAL
    assert result["ok"] is False
    assert result["side_effects"] == [{"connector": "gmail", "tool": "send_email"}]
    # The resolved inputs, because that is what the human has to be shown.
    assert result["inputs"] == {"to": "a@b.c"}
    assert not outbox.exists()


def test_a_parked_run_is_not_left_for_the_agent_to_approve(
    mailer: tuple[RunlacePaths, Connection, Path]
) -> None:
    """The hint has to say who answers, or the model will answer for them."""
    paths, conn, _ = mailer

    async def scenario() -> dict[str, Any]:
        return await with_a_worker(
            paths,
            conn,
            lambda: run_workflow(conn, paths, workflow="mailer", inputs={"to": "a@b.c"}),
        )

    hint = str(asyncio.run(scenario())["hint"])

    assert "cannot approve it yourself" in hint
    assert "gmail.send_email" in hint


def test_approving_a_parked_run_makes_the_side_effect_happen(
    mailer: tuple[RunlacePaths, Connection, Path]
) -> None:
    """The acceptance criterion: parks, is approved, runs, and is journaled."""
    paths, conn, outbox = mailer

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        async def body() -> tuple[dict[str, Any], dict[str, Any]]:
            parked = await run_workflow(
                conn, paths, workflow="mailer", inputs={"to": "a@b.c"}
            )
            run_id = str(parked["run_id"])
            assert approve(conn, run_id)["status"] == QUEUED
            return parked, await until_finished(conn, run_id)

        return await with_a_worker(paths, conn, body)

    parked, done = asyncio.run(scenario())

    assert done["run_id"] == parked["run_id"]
    assert done["status"] == COMPLETED
    assert done["output"] == {"sent": "a@b.c"}
    assert outbox.read_text(encoding="utf-8") == "a@b.c\thi\thello\n"

    # The whole sequence is in the journal, the tool call included.
    steps = done["steps"]
    assert [(s["connector"], s["tool"], s["status"]) for s in steps] == [
        ("gmail", "send_email", "ok")
    ]
    row = find_run(conn, str(done["run_id"]))
    assert row is not None
    assert row["approved_at"] is not None
    assert row["queued_at"] is not None


def test_rejecting_a_parked_run_ends_it_and_nothing_is_sent(
    mailer: tuple[RunlacePaths, Connection, Path]
) -> None:
    paths, conn, outbox = mailer

    async def scenario() -> dict[str, Any]:
        async def body() -> dict[str, Any]:
            parked = await run_workflow(
                conn, paths, workflow="mailer", inputs={"to": "a@b.c"}
            )
            run_id = str(parked["run_id"])
            assert reject(conn, run_id, "wrong address")["status"] == REJECTED
            # Long enough that a worker with any interest in it would have run
            # it by now. It has none: rejected is terminal and never claimable.
            await asyncio.sleep(0.5)
            return get_run(conn, run_id)

        return await with_a_worker(paths, conn, body)

    done = asyncio.run(scenario())

    assert done["status"] == REJECTED
    assert done["finished"] is True
    assert done["ok"] is False
    assert done["approval_note"] == "wrong address"
    assert not outbox.exists()


def test_allow_runs_it_without_asking_anyone(
    mailer: tuple[RunlacePaths, Connection, Path]
) -> None:
    """The developer's call, made where they start Runlace. Not the agent's."""
    paths, conn, outbox = mailer

    async def scenario() -> dict[str, Any]:
        return await with_a_worker(
            paths,
            conn,
            lambda: run_workflow(
                conn,
                paths,
                workflow="mailer",
                inputs={"to": "a@b.c"},
                approval=APPROVAL_ALLOW,
            ),
        )

    result = asyncio.run(scenario())

    assert result["status"] == COMPLETED
    assert outbox.read_text(encoding="utf-8") == "a@b.c\thi\thello\n"


def test_confirm_still_means_what_it_meant(
    mailer: tuple[RunlacePaths, Connection, Path]
) -> None:
    """v1's gate is untouched: approval obtained out of band, in the conversation."""
    paths, conn, outbox = mailer

    async def scenario() -> dict[str, Any]:
        return await with_a_worker(
            paths,
            conn,
            lambda: run_workflow(
                conn, paths, workflow="mailer", inputs={"to": "a@b.c"}, confirm=True
            ),
        )

    result = asyncio.run(scenario())

    assert result["status"] == COMPLETED
    assert outbox.read_text(encoding="utf-8") == "a@b.c\thi\thello\n"


def test_pending_is_how_a_product_finds_what_needs_answering(
    mailer: tuple[RunlacePaths, Connection, Path]
) -> None:
    """The answer run_workflow gave may be long gone; the run row is not."""
    paths, conn, _ = mailer

    async def scenario() -> str:
        async def body() -> str:
            parked = await run_workflow(
                conn, paths, workflow="mailer", inputs={"to": "a@b.c"}
            )
            return str(parked["run_id"])

        return await with_a_worker(paths, conn, body)

    run_id = asyncio.run(scenario())
    waiting = pending(conn)

    assert [w["run_id"] for w in waiting] == [run_id]
    assert waiting[0]["side_effects"] == [{"connector": "gmail", "tool": "send_email"}]
    assert waiting[0]["inputs"] == {"to": "a@b.c"}
