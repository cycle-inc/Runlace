"""What `get_skill` and `get_tools` return.

Two halves. The *static* half is ``SKILL.md``, shipped next to this module: the
calling convention, the file contract, the lint codes and three worked examples.
Every example in it is compiled by ``tests/test_skill_examples.py``, so the
document cannot drift away from the compiler without the suite going red.

The *live* half is generated from this machine's discovery, and is what makes
the difference between an agent guessing at tool names and knowing them. It
comes in two sizes on purpose:

``get_skill`` returns the index -- every tool's name, one line, and its risk.
That is what you need to *choose*. ``get_tools`` returns the signatures of the
few you chose, which is what you need to *call*. Returning every signature up
front looked harmless with a test server and stopped being harmless the day a
real one turned up: one GitHub connector is 47 tools and 8,000 tokens of types,
almost none of them relevant to the workflow being written. The stub files on
disk are unaffected -- pyright reads those, and it does not have a context
window.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .db import Connection
from .stubs import ConnectorSpec, ToolSpec, render_connector_stub

SKILL_FILE = Path(__file__).parent / "SKILL.md"


def read_skill() -> str:
    """SKILL.md as shipped. Read on every call so an edit needs no restart."""
    return SKILL_FILE.read_text(encoding="utf-8")


def build_skill(conn: Connection) -> dict[str, Any]:
    """The skill document plus this machine's live connector index."""
    return {
        "skill": read_skill(),
        "connectors": connector_index(conn),
        "next": (
            "This index has the names. Before you write a call, get_tools("
            "connector, tools) for the exact signature and return type of the "
            "few tools you picked."
        ),
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


def tool_types(
    conn: Connection, connector: str, tools: list[str] | None = None
) -> dict[str, Any]:
    """The generated types for a few tools: what ``get_tools`` returns.

    This is a slice of the same ``.pyi`` pyright checks against, rendered from
    the same code, so it cannot describe a signature the compiler would then
    reject. ``tools`` accepts either spelling -- the MCP name (``get-sum``) or
    the method it became (``get_sum``) -- because the index shows both and an
    agent will copy whichever is nearer.
    """
    row = conn.execute(
        "SELECT name, attr, status FROM connectors WHERE name = ? OR attr = ?",
        (connector, connector),
    ).fetchone()
    if row is None:
        return {
            "ok": False,
            "code": "unknown-connector",
            "error": f"no connector called `{connector}`",
            "hint": "Call get_skill for the connectors on this machine.",
        }

    name, attr = str(row["name"]), str(row["attr"])
    rows = conn.execute(
        "SELECT name, method, description, input_schema_json, output_schema_json, "
        "risk FROM tools WHERE connector = ? ORDER BY name",
        (name,),
    ).fetchall()

    wanted = set(tools or [])
    selected = [r for r in rows if not wanted or {r["name"], r["method"]} & wanted]
    if wanted and not selected:
        return {
            "ok": False,
            "code": "unknown-tool",
            "error": f"{name} has none of: {', '.join(sorted(wanted))}",
            "hint": f"Call get_skill; {name} has {len(rows)} tool(s).",
        }

    matched = {str(r["name"]) for r in selected} | {str(r["method"]) for r in selected}
    spec = ConnectorSpec(
        name=name,
        attr=attr,
        tools=[
            ToolSpec(
                name=str(r["name"]),
                method=str(r["method"]),
                description=r["description"],
                input_schema=_schema(r["input_schema_json"]),
                output_schema=_schema(r["output_schema_json"]),
                risk=str(r["risk"]),
            )
            for r in selected
        ],
    )
    source, warnings = render_connector_stub(spec)

    return {
        "ok": True,
        "connector": name,
        "attr": attr,
        "types": source,
        "tools": [
            {
                "tool": str(r["name"]),
                "call": f"ctx.{attr}.{r['method']}(...)",
                "risk": str(r["risk"]),
            }
            for r in selected
        ],
        "unknown": sorted(wanted - matched),
        "warnings": warnings,
    }


def _schema(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, str) or not raw:
        return None
    loaded = json.loads(raw)
    return loaded if isinstance(loaded, dict) else None


def _one_line(description: Any) -> str:
    if not isinstance(description, str):
        return ""
    return " ".join(description.split())[:200]
