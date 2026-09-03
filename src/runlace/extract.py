"""Stage 3 of the compiler (D3): static tool extraction.

The lint has already guaranteed that ``ctx`` is never rebound, aliased or
passed anywhere, and that every ``ctx.<connector>`` continues into a
``.<tool>(...)`` call. That means reading the tool calls off the AST is exact:
there is no reachable call site this misses, and no call site it invents.

The names produced here are *Python* spellings (``ctx.everything.echo`` gives
``("everything", "echo")``). Mapping them back to the verbatim MCP names is the
compiler's job, because only the database knows that mapping.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from .lint import CTX_PARAM, INPUTS_ATTR


@dataclass(frozen=True)
class ToolCall:
    """One `ctx.<connector>.<method>(...)` call site."""

    connector: str
    method: str
    line: int


def extract_tool_calls(code: str) -> list[ToolCall]:
    """Return every tool call in source order, one entry per call site.

    A tool called from three places appears three times; de-duplicating is left
    to the caller so it can report each line.
    """
    tree = ast.parse(code)
    calls: list[ToolCall] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        tool_access = node.func
        if not isinstance(tool_access, ast.Attribute):
            continue
        connector_access = tool_access.value
        if not isinstance(connector_access, ast.Attribute):
            continue
        if not (
            isinstance(connector_access.value, ast.Name)
            and connector_access.value.id == CTX_PARAM
        ):
            continue
        if connector_access.attr == INPUTS_ATTR:
            # `ctx.inputs` is a dict, not a connector, so `ctx.inputs.get(k, d)`
            # is ordinary Python that the lint deliberately allows. Reading it
            # as a call to a connector named `inputs` rejects the most natural
            # way to write an optional input.
            continue
        calls.append(
            ToolCall(
                connector=connector_access.attr,
                method=tool_access.attr,
                line=node.lineno,
            )
        )
    calls.sort(key=lambda c: (c.line, c.connector, c.method))
    return calls


def unique_tools(calls: list[ToolCall]) -> list[tuple[str, str]]:
    """`(connector, method)` pairs, de-duplicated, in a stable order."""
    return sorted({(c.connector, c.method) for c in calls})
