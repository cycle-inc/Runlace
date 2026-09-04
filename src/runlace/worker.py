"""The thing that drains the queue.

A workflow subprocess cannot outlive the process that spawned it: it is not
independent, every ``ctx.<connector>.<tool>()`` call travels back over a pipe to
its parent, which makes the real MCP call. Detaching it leaves it talking into a
closed pipe. So "run it in the background" cannot mean "stop waiting for it" --
it has to mean *something long-lived owns execution*, and this module is that
something.

Which is why the worker belongs to the daemon and not to the MCP transport. Over
stdio the server dies when the host disconnects, and a run left going there
would be killed mid-flight; over ``--http`` the process is already a daemon and
can see a three-minute workflow through.

One worker per home. Two ``runlace serve`` on the same ``~/.runlace`` would both
claim runs, and while claiming is safe -- see ``claim_queued_run`` -- running the
same side effect twice because somebody left a second daemon open is not a
failure mode worth having.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import (
    Connection,
    claim_queued_run,
    connect,
    find_version_by_id,
    queue_lock_holder,
    release_queue_lock,
    take_queue_lock,
)
from .paths import RunlacePaths
from .policy import read_policy
from .queue import (
    DEFAULT_APPROVAL_TTL_SECONDS,
    HEARTBEAT_SECONDS,
    expire,
    stale_before,
)
from .runs import Admitted, execute, side_effects_of
from .workflows import get_workflow

log = logging.getLogger("runlace.worker")

# How many workflows may execute at once. Each one is a Python subprocess and a
# handful of open MCP sessions, so this is a memory number as much as a speed
# one; four is enough that a slow report does not block a quick lookup, and few
# enough to be unsurprising on a laptop.
DEFAULT_CONCURRENCY = 4

# How long the worker waits before looking at an empty queue again. Short: this
# is one indexed SELECT, and the cost of getting it wrong is latency on every
# single run.
POLL_SECONDS = 0.25


class Worker:
    """Claims queued runs and executes them, until told to stop."""

    def __init__(
        self,
        paths: RunlacePaths,
        *,
        concurrency: int = DEFAULT_CONCURRENCY,
        poll_seconds: float = POLL_SECONDS,
        approval_ttl_seconds: float = DEFAULT_APPROVAL_TTL_SECONDS,
    ) -> None:
        self.paths = paths
        self.concurrency = concurrency
        self.poll_seconds = poll_seconds
        self.approval_ttl_seconds = approval_ttl_seconds
        self.owner_pid = os.getpid()
        self.hostname = socket.gethostname()

    async def drain(self, stop: asyncio.Event) -> None:
        """Work the queue until ``stop`` is set. Returns when in-flight runs end.

        Does nothing but return if another daemon already holds the lock: a
        second server on the same home is still perfectly good at answering
        questions, it just must not also be executing.
        """
        conn = connect(self.paths.db)
        try:
            if not self._heartbeat(conn):
                holder = queue_lock_holder(conn)
                log.warning(
                    "not draining: pid %s on %s already owns this queue",
                    holder["owner_pid"] if holder else "?",
                    holder["hostname"] if holder else "?",
                )
                return
            log.info("draining %s, up to %s at a time", self.paths.db, self.concurrency)
            await self._loop(conn, stop)
        finally:
            release_queue_lock(
                conn, owner_pid=self.owner_pid, hostname=self.hostname
            )
            conn.close()

    async def _loop(self, conn: Connection, stop: asyncio.Event) -> None:
        running: set[asyncio.Task[None]] = set()
        last_beat = _now()
        try:
            while not stop.is_set():
                if (_now() - last_beat).total_seconds() >= HEARTBEAT_SECONDS:
                    last_beat = _now()
                    self._heartbeat(conn)
                    # Piggybacked on the heartbeat rather than given its own
                    # timer: it is the same "once in a while" and one clock is
                    # easier to reason about than two.
                    for run_id in expire(conn, ttl_seconds=self.approval_ttl_seconds):
                        log.info("gave up on %s: nobody approved it", run_id)

                while len(running) < self.concurrency:
                    row = claim_queued_run(conn)
                    if row is None:
                        break
                    task = asyncio.create_task(self._execute(str(row["id"])))
                    running.add(task)
                    task.add_done_callback(running.discard)

                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self.poll_seconds)
        finally:
            # A run that started must be allowed to finish. Killing a workflow
            # halfway through its side effects to shut down half a second sooner
            # is the worst trade in the system.
            if running:
                log.info("waiting for %s run(s) to finish", len(running))
                await asyncio.gather(*running, return_exceptions=True)

    async def _execute(self, run_id: str) -> None:
        """One claimed run, on its own connection so two never share a transaction."""
        conn = connect(self.paths.db)
        try:
            admitted = self._reconstitute(conn, run_id)
            if admitted is None:
                return
            await execute(conn, self.paths, admitted)
        except Exception:  # noqa: BLE001 - a worker that dies on one run drains nothing
            log.exception("run %s crashed the worker loop", run_id)
        finally:
            conn.close()

    def _reconstitute(self, conn: Connection, run_id: str) -> Admitted | None:
        """Rebuild what ``admit`` decided, from the row it wrote.

        The inputs come back off the run and not out of the workflow's defaults:
        they are what was validated, and for a parked run they are what a human
        was shown. Nothing between admission and here may change them. Same for
        the side effects -- a run approved on the strength of "this sends one
        email" must not act on a `policy.yaml` edited since.
        """
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            log.error("claimed run %s and then could not find it", run_id)
            return None
        version = find_version_by_id(conn, str(row["workflow_version_id"]))
        if version is None:
            log.error("run %s points at a version that is gone", run_id)
            return None
        record = get_workflow(
            conn, str(version["workflow_id"]), version=str(version["id"])
        )
        if record is None or not isinstance(record.get("code"), str):
            log.error("run %s has no code on disk to execute", run_id)
            return None

        inputs: dict[str, Any] = json.loads(str(row["inputs_json"] or "{}"))
        stored = row["side_effects_json"]
        return Admitted(
            run_id=run_id,
            record=record,
            inputs=inputs,
            dry_run=bool(row["dry_run"]),
            # NULL for a run written before the column existed, and for one with
            # no side effects at all. Recomputing gives the right answer in the
            # second case and the best available one in the first.
            side_effects=(
                json.loads(str(stored))
                if stored is not None
                else side_effects_of(read_policy(self.paths.policy), record)
            ),
        )

    def _heartbeat(self, conn: Connection) -> bool:
        return take_queue_lock(
            conn,
            owner_pid=self.owner_pid,
            hostname=self.hostname,
            stale_before=stale_before(),
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)
