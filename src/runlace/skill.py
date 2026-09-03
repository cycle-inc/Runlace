"""What `get_skill` returns.

Two halves. The *live* half -- the connector index and the ``.pyi`` excerpts --
is generated from this machine's discovery, and is what makes the difference
between an agent guessing at tool names and knowing them. The *static* half is
SKILL.md, which is written in M4; until then this module ships a short primer
covering the calling convention and the file contract, so the tool is usable.
"""

from __future__ import annotations

from typing import Any

from .db import Connection
from .paths import RunlacePaths

PRIMER = """\
# Writing a Runlace workflow

Runlace turns a piece of Python into a stored, replayable workflow over the MCP
servers this machine is connected to. You write it once; afterwards it runs with
no model in the loop.

## The loop

1. `get_skill` -- you are here. Read the connector index below for the tools you
   actually have.
2. `create_workflow` -- send the code. It is compiled: linted, typechecked with
   pyright against generated stubs, and its tool calls extracted. If it fails you
   get the stage, the line and a hint; fix and send again.
3. `run_workflow` -- execute it. If the workflow touches any side-effecting tool,
   the run is refused until you show the human which tools will act and pass
   `confirm=True`.

## The calling convention

You never import connectors. You receive `ctx`. Call tools as
`ctx.<connector>.<tool>(**kwargs)`, for example
`ctx.pennylane.list_transactions(from_="2024-01-01", to="2024-01-31")`. Runlace
resolves the call to the real MCP server at run time.

Use static attribute access only. `getattr(ctx, name)` is rejected, and so is
storing `ctx`, a connector or a tool in a variable: the tools a workflow uses are
read off the source, so every call has to be spelled out.

Arguments are keyword-only. A JSON key that is a Python reserved word gets a
trailing underscore in the stub (`from` becomes `from_`) and is mapped back at
run time.

## The file contract

```python
from runlace_types import Ctx, Output

def run(ctx: Ctx) -> Output:
    txs = ctx.pennylane.list_transactions(from_=ctx.inputs["from"], to=ctx.inputs["to"])
    return {"count": len(txs)}
```

- Exactly one top-level `def run(ctx)`. `async def run` is rejected.
- Annotate the return type. Use `-> Output` when you declare an `outputs_schema`
  (`Output` is generated from it, so pyright checks what you return); use
  `-> dict[str, object]` when you do not.
- `ctx.inputs` is subscripted with the raw JSON key from your `inputs_schema`.
- Imports are limited to: json, datetime, re, math, collections, itertools,
  statistics, decimal, dataclasses, typing -- plus `runlace_types`.
- Tools with no output schema return `object`. Narrow explicitly before using
  the result, or pyright will reject the workflow.

## The inputs rule

Inline constants are fine when they define the workflow itself -- a fixed board
ID, a URL. Anything a user might vary between runs -- dates, recipients,
amounts, filters -- must be a declared input, with a sensible default where
possible.

Good: `inputs_schema` declares `month`, and the code reads `ctx.inputs["month"]`.
Bad: the code hard-codes `"2024-01"` and the workflow has to be recreated next
month.

## Forbidden patterns

Rejected at lint, each with its own error name: `forbidden-import`
(subprocess, os, sys, socket, http, urllib, requests, httpx, aiohttp,
importlib), `import-not-allowed` (anything else off the allowlist),
`forbidden-call` (open, exec, eval, `__import__`),
`dynamic-attribute-access` (getattr, setattr, delattr, vars, globals, locals),
`dunder-access`, `async-not-supported`, `ctx-escape`, `ctx-rebound`,
`ctx-tool-not-called`, `missing-run`, `bad-run-signature`,
`missing-return-annotation`, `bad-output-annotation`.
"""


def build_skill(conn: Connection, paths: RunlacePaths) -> dict[str, Any]:
    """The skill document plus this machine's live connector index and stubs."""
    return {
        "skill": PRIMER,
        "connectors": connector_index(conn),
        "stubs": stub_excerpts(paths),
    }


def connector_index(conn: Connection) -> list[dict[str, Any]]:
    """Every connector, and for the reachable ones every tool with its risk."""
    connectors: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT name, attr, status, detail FROM connectors ORDER BY name"
    ):
        tools = [
            {
                "tool": str(t["name"]),
                "call": f"ctx.{row['attr']}.{t['method']}(...)",
                "description": _one_line(t["description"]),
                "risk": str(t["risk"]),
            }
            for t in conn.execute(
                "SELECT name, method, description, risk FROM tools "
                "WHERE connector = ? ORDER BY name",
                (str(row["name"]),),
            )
        ]
        connectors.append(
            {
                "connector": str(row["name"]),
                "attr": str(row["attr"]),
                "status": str(row["status"]),
                "detail": row["detail"],
                "tools": tools,
            }
        )
    return connectors


def stub_excerpts(paths: RunlacePaths) -> dict[str, str]:
    """The generated ``.pyi`` files, keyed by their path relative to the home."""
    if not paths.types.exists():
        return {}
    excerpts: dict[str, str] = {}
    for path in sorted(paths.types.rglob("*.pyi")):
        if path.name == "__init__.pyi":
            continue
        excerpts[str(path.relative_to(paths.home))] = path.read_text(encoding="utf-8")
    return excerpts


def _one_line(description: Any) -> str:
    if not isinstance(description, str):
        return ""
    return " ".join(description.split())[:200]
