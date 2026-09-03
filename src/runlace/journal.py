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
"""

from __future__ import annotations

import json
from typing import Any

from .db import Connection, find_run, find_step, list_steps

CODE_UNKNOWN_RUN = "unknown-run"
CODE_UNKNOWN_STEP = "unknown-step"

# Two, not one: the first item shows the shape and the second shows which of
# its fields were optional after all. A third is the same shape a third time --
# on the largest step in a real journal, going from three to two took the
# answer from 2,781 tokens to 1,981 without losing anything an agent needs.
MAX_ITEMS = 2
MAX_STRING = 300


def get_step(conn: Connection, run_id: str, seq: int) -> dict[str, Any]:
    """One journaled tool call, arguments and result included, trimmed."""
    row = find_step(conn, run_id, seq)
    if row is None:
        return _no_step(conn, run_id, seq)

    notes: list[str] = []
    payload = _trim(_decode(row["payload_json"]), "payload", notes)
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
