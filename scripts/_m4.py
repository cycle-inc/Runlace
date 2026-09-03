"""Shared by the steps of `m4_acceptance.sh`, which each run as their own process."""

from __future__ import annotations

from typing import Any

from _mcp import call as call  # re-exported: the steps import everything from here

# Written against nothing but what `get_skill` returned: the connector index
# gives `ctx.everything.echo` and `ctx.everything.get_sum`, and the stub gives
# their parameters and the fact that both are read-only.
CODE = """\
from runlace_types import Ctx


def run(ctx: Ctx) -> dict[str, object]:
    greeting = ctx.everything.echo(message=ctx.inputs["message"])
    total = ctx.everything.get_sum(a=ctx.inputs["a"], b=ctx.inputs["b"])
    return {"greeting": str(greeting), "total": str(total)}
"""

INPUTS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "message": {"type": "string"},
        "a": {"type": "number"},
        "b": {"type": "number"},
    },
    "required": ["message", "a", "b"],
}

ARGUMENTS: dict[str, Any] = {"message": "good morning", "a": 20, "b": 22}
