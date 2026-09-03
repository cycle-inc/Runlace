"""What `get_skill` returns.

Two halves. The *static* half is ``SKILL.md``, shipped next to this module: the
calling convention, the file contract, the lint codes and three worked examples.
Every example in it is compiled by ``tests/test_skill_examples.py``, so the
document cannot drift away from the compiler without the suite going red.

The *live* half -- the connector index and the ``.pyi`` excerpts -- is generated
from this machine's discovery, and is what makes the difference between an agent
guessing at tool names and knowing them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .db import Connection
from .paths import RunlacePaths

SKILL_FILE = Path(__file__).parent / "SKILL.md"


def read_skill() -> str:
    """SKILL.md as shipped. Read on every call so an edit needs no restart."""
    return SKILL_FILE.read_text(encoding="utf-8")


def build_skill(conn: Connection, paths: RunlacePaths) -> dict[str, Any]:
    """The skill document plus this machine's live connector index and stubs."""
    return {
        "skill": read_skill(),
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
