"""The workflow the M6 acceptance authors, and the bug it is written around.

The bug matters more than the workflow. It has to be one the compiler cannot
see -- pyright is happy with `sum(over) / len(over)`, because nothing in the
types says the list can be empty -- and one that real data actually triggers.
That is the gap a dry run exists to cover, and the only honest way to show it is
to walk into it.
"""

from __future__ import annotations

from typing import Any

from _mcp import call as call  # re-exported: the steps import everything from here

CITIES = ["New York", "Chicago", "Los Angeles"]

# `over` is empty whenever no city is above the limit, and then the average
# divides by zero. Two reads happen before it, so a dry run gets all the way
# there on real data.
CODE = """\
from runlace_types import Ctx, Output


def run(ctx: Ctx) -> Output:
    \"\"\"Average the cities that are over the limit, and log it when any are.\"\"\"
    limit = ctx.inputs["limit"]

    here = ctx.everything.get_structured_content(location=ctx.inputs["first"])
    there = ctx.everything.get_structured_content(location=ctx.inputs["second"])

    over = [t for t in (here["temperature"], there["temperature"]) if t > limit]
    if over:
        ctx.everything.toggle_simulated_logging()

    return {"over": len(over), "average_over": sum(over) / len(over)}
"""

BUG = '"over": len(over), "average_over": sum(over) / len(over)'
FIX = '"over": len(over), "average_over": sum(over) / len(over) if over else 0.0'

INPUTS: dict[str, Any] = {
    "type": "object",
    "properties": {
        # The tool's own parameter is an enum, so the input has to be one too:
        # the stub types it as a Literal, and a plain string would not typecheck.
        "first": {"type": "string", "enum": CITIES},
        "second": {"type": "string", "enum": CITIES},
        "limit": {"type": "number", "description": "Celsius above which to alert."},
    },
    "required": ["first", "second", "limit"],
}

OUTPUTS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "over": {"type": "integer"},
        "average_over": {"type": "number"},
    },
    "required": ["over", "average_over"],
}

# Nothing on Earth is over 200C, so `over` is empty and the bug fires.
NOBODY_OVER: dict[str, Any] = {"first": "New York", "second": "Chicago", "limit": 200}

# Everything is over -50C, so the side effect is reached on every run.
EVERYONE_OVER: dict[str, Any] = {"first": "New York", "second": "Chicago", "limit": -50}
