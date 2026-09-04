"""Reading back what a run recorded.

``run_workflow`` reports each step without its payload or its result -- a step
that read a thousand rows would drown the agent's context, which is the reason
the workflow ran in a subprocess in the first place. Both sides are in the
journal all the same, and sometimes you need one: a dry run crashed on a field
that was not there, and the only way to find out what *is* there is to look.

So this module hands back one step, trimmed. Long lists are cut to their first
few items and long strings to their first few hundred characters, with a note
saying what was dropped and from where. That is deliberate and there is no flag
to turn it off: the point is to show an agent the *shape* of what a tool
returned, so it can write code against it. If it needs all thousand rows, the
workflow is the thing that should be reading them.

The arguments are treated differently, and the difference is on purpose -- see
``PAYLOAD_BUDGET``.
"""

from __future__ import annotations

import json
from typing import Any

from .db import (
    Connection,
    find_run,
    find_step,
    find_version_by_id,
    find_workflow,
    list_steps,
)
from .queue import AWAITING_APPROVAL, COMPLETED, TERMINAL

CODE_UNKNOWN_RUN = "unknown-run"
CODE_UNKNOWN_STEP = "unknown-step"

# Two, not one: the first item shows the shape and the second shows which of
# its fields were optional after all. A third is the same shape a third time --
# on the largest step in a real journal, going from three to two took the
# answer from 2,781 tokens to 1,981 without losing anything an agent needs.
MAX_ITEMS = 2
MAX_STRING = 300

# The arguments are the workflow's own, not foreign data of unknown size, and
# an agent debugging a call needs to see exactly what it sent -- trimming
# `fields=["name", "path", "repository"]` down to two hides the answer. So they
# come back verbatim until they are big enough to be the problem themselves: a
# 50 KB `body=`, a bulk create with five hundred items.
PAYLOAD_BUDGET = 2000


def get_run(conn: Connection, run_id: str) -> dict[str, Any]:
    """Where one run got to, and what it produced. The same shape as run_workflow.

    Deliberately the same shape: a caller that stopped waiting and came back
    later should not have to read a second kind of answer to learn the same
    thing. ``steps`` carries no payloads, for the same reason it never does --
    ``get_step`` is how you look inside one.
    """
    row = find_run(conn, run_id)
    if row is None:
        return {
            "ok": False,
            "code": CODE_UNKNOWN_RUN,
            "error": f"no run called `{run_id}`",
            "hint": "`run_id` comes back from run_workflow and dry_run_workflow, "
            "on refusals as well as on successes.",
        }

    status = str(row["status"])
    result: dict[str, Any] = {
        **_whose_run(conn, row),
        "run_id": run_id,
        "ok": status == COMPLETED,
        "status": status,
        "finished": status in TERMINAL,
        "dry_run": bool(row["dry_run"]),
        "output": _decode(row["output_json"]),
        "error": row["error"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "steps": [_step_summary(step) for step in list_steps(conn, run_id)],
    }
    if status == AWAITING_APPROVAL:
        # What it wants to act with, and what it would act on. A developer's app
        # listing pending approvals renders these; it does not have the answer
        # run_workflow gave, and may not even be the process that got it.
        result["side_effects"] = _decode(row["side_effects_json"]) or []
        result["inputs"] = _decode(row["inputs_json"]) or {}
        result["hint"] = (
            "It is waiting for a human to approve it. Nothing has run and "
            "nothing will until approve_run is called with this run_id."
        )
    elif status not in TERMINAL:
        result["hint"] = f"It is `{status}`. Ask again in a moment."
    if row["approval_note"] is not None:
        result["approval_note"] = row["approval_note"]
    return result


def _whose_run(conn: Connection, row: Any) -> dict[str, Any]:
    """The workflow a run belongs to. A run row knows only its version."""
    version = find_version_by_id(conn, str(row["workflow_version_id"]))
    if version is None:
        return {"workflow_id": None, "name": None, "version": None}
    workflow = find_workflow(conn, str(version["workflow_id"]))
    return {
        "workflow_id": str(version["workflow_id"]),
        "name": str(workflow["name"]) if workflow is not None else None,
        "version": str(version["version_hash"]),
    }


def _step_summary(step: Any) -> dict[str, Any]:
    """The same seven fields ``run_workflow`` reports, read back off the journal."""
    return {
        "seq": int(step["seq"]),
        "connector": str(step["connector"]),
        "tool": str(step["tool"]),
        "risk": str(step["risk"]),
        "status": str(step["status"]),
        "duration_ms": step["duration_ms"],
        "error": step["error"],
    }


def get_step(conn: Connection, run_id: str, seq: int) -> dict[str, Any]:
    """One journaled tool call, arguments and result included, trimmed."""
    row = find_step(conn, run_id, seq)
    if row is None:
        return _no_step(conn, run_id, seq)

    notes: list[str] = []
    payload = _decode(row["payload_json"])
    if len(row["payload_json"] or "") > PAYLOAD_BUDGET:
        payload = _trim(payload, "payload", notes)
    result = _trim(_decode(row["result_json"]), "result", notes)

    return {
        "ok": True,
        "run_id": run_id,
        "seq": seq,
        "connector": str(row["connector"]),
        "tool": str(row["tool"]),
        "risk": str(row["risk"]),
        "status": str(row["status"]),
        "duration_ms": row["duration_ms"],
        "error": row["error"],
        "payload": payload,
        "result": result,
        # What the tool actually returned, before trimming. A step whose result
        # is 40,000 characters is the argument for aggregating in the workflow.
        "result_chars": len(row["result_json"] or ""),
        "trimmed": notes,
    }


def _no_step(conn: Connection, run_id: str, seq: int) -> dict[str, Any]:
    """Distinguish "no such run" from "that run has no step 7"."""
    if find_run(conn, run_id) is None:
        return {
            "ok": False,
            "code": CODE_UNKNOWN_RUN,
            "error": f"no run called `{run_id}`",
            "hint": "`run_id` comes back from run_workflow and dry_run_workflow, "
            "on refusals as well as on successes.",
        }
    available = [int(step["seq"]) for step in list_steps(conn, run_id)]
    return {
        "ok": False,
        "code": CODE_UNKNOWN_STEP,
        "error": f"run `{run_id}` has no step {seq}",
        "hint": (
            f"Its steps are numbered {available[0]} to {available[-1]}."
            if available
            else "That run made no tool calls at all -- it was refused before it "
            "ran, or the workflow calls nothing."
        ),
        "steps": available,
    }


def _decode(column: Any) -> Any:
    if not isinstance(column, str):
        return None
    try:
        return json.loads(column)
    except json.JSONDecodeError:
        return column


def _trim(value: Any, path: str, notes: list[str]) -> Any:
    """Cut the value down, keeping its type at every level.

    Nothing is replaced by an ellipsis marker: a "... 47 more" string sitting
    in a list of objects would misreport the very shape this is here to show.
    What was dropped is said out of band, in ``notes``.
    """
    if isinstance(value, str):
        if len(value) > MAX_STRING:
            notes.append(f"{path}: kept {MAX_STRING} of {len(value)} characters")
            return value[:MAX_STRING]
        return value
    if isinstance(value, list):
        items: list[Any] = value  # pyright: ignore[reportUnknownVariableType]
        kept = [_trim(v, f"{path}[{i}]", notes) for i, v in enumerate(items[:MAX_ITEMS])]
        if len(items) > MAX_ITEMS:
            notes.append(f"{path}: kept {MAX_ITEMS} of {len(items)} items")
        return kept
    if isinstance(value, dict):
        fields: dict[str, Any] = {str(k): v for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
        return {k: _trim(v, f"{path}.{k}", notes) for k, v in fields.items()}
    return value
