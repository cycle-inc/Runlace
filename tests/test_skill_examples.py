"""SKILL.md is documentation the compiler has to agree with.

A skill file that teaches a pattern the compiler rejects is worse than no skill
file: the agent follows it, gets an error, and has no way to tell which of the
two is wrong. So every workflow in SKILL.md goes through `compile_workflow`
here, and the lint codes it advertises are checked against the ones that exist.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from runlace import lint
from runlace.compiler import compile_workflow
from runlace.db import Connection
from runlace.paths import RunlacePaths
from runlace.skill import SKILL_FILE, read_skill

FENCE = re.compile(r"^```(\w+)\n(.*?)^```", re.MULTILINE | re.DOTALL)

# The snippets that illustrate the contract rather than a whole
# `create_workflow` call have no schema block of their own. They still have to
# compile, so they get one covering every input the document mentions, all
# required -- `ctx.inputs["from"]` on an optional key is exactly the strict-mode
# error the document warns about.
FALLBACK_INPUTS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "from": {"type": "string"},
        "to": {"type": "string"},
        "email": {"type": "string"},
        "note": {"type": "string"},
        "threshold": {"type": "number"},
    },
    "required": ["from", "to", "email", "note", "threshold"],
}


def examples() -> list[tuple[dict[str, Any], str]]:
    """Every `def run` block in SKILL.md, with the schemas declared above it."""
    found: list[tuple[dict[str, Any], str]] = []
    args: dict[str, Any] = {}
    for language, body in FENCE.findall(read_skill()):
        if language == "json":
            args = json.loads(body)
        elif language == "python" and "def run(" in body:
            found.append((args, body))
            args = {}
    return found


EXAMPLES = examples()


def test_the_document_holds_the_examples_the_spec_asks_for() -> None:
    """Three complete examples, plus the contract snippet, inside the budget.

    The budget is context an agent pays for on every `get_skill`, so it is a
    real ceiling and not a style rule. It was 400 lines until `ctx.ai` arrived;
    a feature that changes what a workflow can be is worth the 35.
    """
    assert len(EXAMPLES) >= 4
    assert len(read_skill().splitlines()) < 440


@pytest.mark.parametrize(
    ("args", "code"),
    EXAMPLES,
    ids=[f"example-{i}" for i in range(1, len(EXAMPLES) + 1)],
)
def test_every_example_compiles(
    home: tuple[RunlacePaths, Connection], args: dict[str, Any], code: str
) -> None:
    paths, conn = home
    result = compile_workflow(
        conn,
        paths.types,
        name="skill_example",
        code=code,
        inputs_schema=args.get("inputs_schema", FALLBACK_INPUTS),
        outputs_schema=args.get("outputs_schema"),
    )
    assert result.ok, [str(e) for e in result.errors]


def test_the_advertised_lint_codes_are_the_real_ones() -> None:
    """A code that no longer exists sends the agent looking for the wrong thing."""
    real = {v for k, v in vars(lint).items() if k.startswith("E_")}
    documented = set(re.findall(r"^\| `([a-z-]+)` \|", read_skill(), re.MULTILINE))
    assert documented == real


def test_the_skill_file_ships_with_the_package() -> None:
    """`get_skill` reads it off disk; if packaging drops it, the tool is empty."""
    assert SKILL_FILE.parent.name == "runlace"
    assert SKILL_FILE.is_file()
