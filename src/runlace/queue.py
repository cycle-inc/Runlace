"""The run queue: the states a run can be in, and who is allowed to move it.

A run used to have two states and both were about the past: it completed, or it
failed. Running workflows in the background needs states about the present --
waiting for a worker, waiting for a human -- and the moment a run can wait,
something has to say which waits are legal and who ends them.

There is no queue table. The queue *is* ``runs`` with more states, which is why
a queued run survives a restart for the same reason the audit log does: it was
never anywhere but on disk.

The two-party assumption is what the parked state exists to undo. v1 assumed a
human reading the same transcript the agent writes to, so a side-effecting run
could be refused with "ask them and call again". The shape this is built for is
three deep -- an end user, a chatbot built by a developer, and Runlace inside
that developer's backend -- and in that shape we can neither see the end user
nor reach them. So a run that needs a human is not refused, it is **parked**:
it keeps its inputs, says which tools want to act, and waits for someone else's
product to ask someone we will never meet.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import (
    Connection,
    approve_run,
    close_parked_run,
    find_run,
    list_parked_runs,
    queue_lock_holder,
)
from .runner import STATUS_COMPLETED, STATUS_FAILED

# Waiting on a worker. Claimable.
QUEUED = "queued"
# Waiting on a human. Not claimable, and no worker will ever pick it up on its
# own -- something has to approve it first.
AWAITING_APPROVAL = "awaiting_approval"
# Claimed, executing now.
RUNNING = "running"
COMPLETED = STATUS_COMPLETED
FAILED = STATUS_FAILED
# A human said no.
REJECTED = "rejected"
# Nobody said anything for long enough that we stopped holding the line open.
EXPIRED = "expired"

TERMINAL = frozenset({COMPLETED, FAILED, REJECTED, EXPIRED})

# How long a parked run waits before it is given up on. Long enough that a human
# can go to lunch, short enough that a queue is not a graveyard. The developer
# embedding Runlace overrides it: a chat product wants minutes, an approval that
# routes through email wants a day.
DEFAULT_APPROVAL_TTL_SECONDS = 24 * 60 * 60

# A worker says it is alive this often, and a lock nobody has renewed for three
# of those is abandoned.
HEARTBEAT_SECONDS = 10.0
STALE_AFTER_SECONDS = 3 * HEARTBEAT_SECONDS

# What happens to a side-effecting run that arrives without `confirm`. This is
# the developer's decision and nobody else's: they know whether their product is
# an internal ops bot or a public agent, and those want opposite answers. It is
# deliberately not an MCP tool and not something a workflow can set -- if the
# agent could lift the gate it would lift it for itself, in the same turn it
# wrote the workflow, and the gate would be theatre.
APPROVAL_ASK = "ask"  # park it and wait for a human. The default.
APPROVAL_ALLOW = "allow"  # run it; approval is handled somewhere we cannot see.
APPROVAL_MODES = (APPROVAL_ASK, APPROVAL_ALLOW)

CODE_UNKNOWN_RUN = "unknown-run"
CODE_NOT_PARKED = "not-awaiting-approval"
CODE_AWAITING_APPROVAL = "awaiting-approval"

EXPIRY_NOTE = "no answer before the approval window closed"


def has_live_worker(conn: Connection) -> bool:
    """Is something draining this home's queue right now?

    The question every caller of ``run_workflow`` has to ask, because the answer
    decides whether it can hand a run over or has to run it itself. It lives
    here rather than next to the worker so that the code deciding whether to
    queue does not have to import the thing that would execute the queue.
    """
    holder = queue_lock_holder(conn)
    return holder is not None and str(holder["heartbeat"]) >= stale_before()


def stale_before(now: datetime | None = None) -> str:
    """The heartbeat older than which a worker is presumed gone."""
    moment = now or datetime.now(timezone.utc)
    return (moment - timedelta(seconds=STALE_AFTER_SECONDS)).isoformat(
        timespec="seconds"
    )


def approve(conn: Connection, run_id: str) -> dict[str, Any]:
    """Let a parked run into the queue. A worker picks it up from there.

    Whoever calls this is asserting that a human said yes. Runlace cannot check
    that and does not try -- see the module docstring for why the human is out
    of our reach. What it does guarantee is that the run that executes is the
    one that was described: the inputs were validated and stored when the run
    was parked, and nothing here can change them.
    """
    parked = _parked(conn, run_id)
    if parked is not None:
        return parked
    approve_run(conn, run_id)
    conn.commit()
    return {"ok": True, "run_id": run_id, "status": QUEUED}


def reject(conn: Connection, run_id: str, reason: str | None = None) -> dict[str, Any]:
    """End a parked run because a human said no. It never executes."""
    parked = _parked(conn, run_id)
    if parked is not None:
        return parked
    note = reason or "refused"
    close_parked_run(conn, run_id, status=REJECTED, note=note)
    conn.commit()
    return {"ok": True, "run_id": run_id, "status": REJECTED, "reason": note}


def pending(conn: Connection) -> list[dict[str, Any]]:
    """Every run waiting on a human, oldest first.

    What a developer's product renders as "3 actions need your approval". It
    exists because the answer ``run_workflow`` gave may be long gone: their
    process restarted, or the person who has to answer is not the person who
    asked. The run row is the only durable record, so it is the one to read.
    """
    return [
        {
            "run_id": str(row["id"]),
            "started_at": row["started_at"],
            "inputs": json.loads(str(row["inputs_json"] or "{}")),
            "side_effects": json.loads(str(row["side_effects_json"] or "[]")),
        }
        for row in list_parked_runs(conn)
    ]


def expire(
    conn: Connection, *, ttl_seconds: float = DEFAULT_APPROVAL_TTL_SECONDS
) -> list[str]:
    """Give up on parked runs nobody answered. Returns the ones ended.

    Without this the queue fills with runs whose human closed the tab, and a
    developer reading `list_runs` cannot tell those from the ones still worth
    answering.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)
    ).isoformat(timespec="seconds")
    expired: list[str] = []
    for row in list_parked_runs(conn, created_before=cutoff):
        run_id = str(row["id"])
        if close_parked_run(conn, run_id, status=EXPIRED, note=EXPIRY_NOTE):
            expired.append(run_id)
    conn.commit()
    return expired


def _parked(conn: Connection, run_id: str) -> dict[str, Any] | None:
    """None when the run is parked; the refusal to return otherwise."""
    row = find_run(conn, run_id)
    if row is None:
        return {
            "ok": False,
            "code": CODE_UNKNOWN_RUN,
            "error": f"no run called `{run_id}`",
            "hint": "`run_id` comes back from run_workflow, on parked runs as "
            "well as on finished ones.",
        }
    status = str(row["status"])
    if status == AWAITING_APPROVAL:
        return None
    return {
        "ok": False,
        "code": CODE_NOT_PARKED,
        "error": f"run `{run_id}` is `{status}`, not waiting for approval",
        "hint": _hint_for(status),
        "run_id": run_id,
        "status": status,
    }


def _hint_for(status: str) -> str:
    """Why the answer arrived too late, in the words of the state it found."""
    if status in TERMINAL:
        return (
            "It is already over; approving or refusing it now would change "
            "nothing. Call run_workflow again to run it a second time."
        )
    if status == QUEUED:
        return "It has already been approved and is waiting for a worker."
    return "It is executing right now."
