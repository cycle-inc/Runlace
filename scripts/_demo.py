"""The workflow the demo writes, and the schemas it declares around it.

Two reads and one side effect, against the official `everything` server, so the
demo can show the confirm gate on a tool that really does something rather than
on a tool we pretended about.
"""

from __future__ import annotations

from typing import Any

from _mcp import call as call  # re-exported: the steps import everything from here

CITIES = ["New York", "Chicago", "Los Angeles"]

CODE = """\
from runlace_types import Ctx, Output


def run(ctx: Ctx) -> Output:
    \"\"\"Compare two cities and raise the alarm if either one is over the limit.\"\"\"
    first = ctx.inputs["first"]
    second = ctx.inputs["second"]

    here = ctx.everything.get_structured_content(location=first)
    there = ctx.everything.get_structured_content(location=second)

    hottest = first if here["temperature"] >= there["temperature"] else second
    peak = max(here["temperature"], there["temperature"])

    alerted = peak > ctx.inputs["limit"]
    if alerted:
        ctx.everything.toggle_simulated_logging()

    return {"hottest": hottest, "temperature": peak, "alerted": alerted}
"""

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
        "hottest": {"type": "string"},
        "temperature": {"type": "number"},
        "alerted": {"type": "boolean"},
    },
    "required": ["hottest", "temperature", "alerted"],
}

# The limit is below anything the server reports, so the side effect always
# fires: the demo would be pointless if the gate depended on the weather.
ARGUMENTS: dict[str, Any] = {"first": "New York", "second": "Chicago", "limit": -50}
