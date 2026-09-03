"""Editing a workflow: one exact string, recompiled, stored as a new version.

`edit_workflow` is a shortcut through `create_workflow`, not a second way to
store things, so the two properties worth protecting are that it really does
compile what it produced, and that D1's immutability survives it: nothing is
updated in place, and the version that was edited stays exactly where it was.

The refusals get as much attention as the happy path. An edit that guessed
which of two occurrences was meant would change the wrong line silently, and
silently is the one failure mode a compiler cannot save you from.
"""

from __future__ import annotations

from typing import Any

from runlace.db import Connection
from runlace.paths import RunlacePaths
from runlace.workflows import (
    STAGE_EDIT,
    CreateResult,
    create_workflow,
    edit_workflow,
    get_workflow,
)

INPUTS = {
    "type": "object",
    "properties": {"to": {"type": "string"}},
    "required": ["to"],
}

OUTPUTS = {
    "type": "object",
    "properties": {"to": {"type": "string"}},
    "required": ["to"],
}

BALANCE = (
    "from runlace_types import Ctx\n\n\n"
    "def run(ctx: Ctx) -> dict[str, object]:\n"
    "    ctx.pennylane.get_balance()\n"
    '    return {"to": ctx.inputs["to"]}\n'
)

# The same line twice: nothing can pick between them but the caller.
TWICE = (
    "from runlace_types import Ctx\n\n\n"
    "def run(ctx: Ctx) -> dict[str, object]:\n"
    "    ctx.pennylane.get_balance()\n"
    "    ctx.pennylane.get_balance()\n"
    '    return {"to": ctx.inputs["to"]}\n'
)


def store(
    home: tuple[RunlacePaths, Connection],
    code: str = BALANCE,
    *,
    name: str = "report",
    description: str = "A report.",
    outputs_schema: dict[str, Any] | None = None,
) -> CreateResult:
    paths, conn = home
    result = create_workflow(
        conn,
        paths,
        name=name,
        description=description,
        code=code,
        inputs_schema=INPUTS,
        outputs_schema=outputs_schema,
    )
    assert result.ok, [str(e) for e in result.errors]
    return result


def edit(
    home: tuple[RunlacePaths, Connection],
    old_string: str,
    new_string: str,
    *,
    name: str = "report",
    **rest: Any,
) -> CreateResult:
    paths, conn = home
    return edit_workflow(
        conn, paths, name=name, old_string=old_string, new_string=new_string, **rest
    )


def code_of(conn: Connection, name: str = "report", version: str | None = None) -> str:
    record = get_workflow(conn, name, version=version)
    assert record is not None
    return str(record["code"])


# -- the happy path --------------------------------------------------------


def test_an_edit_stores_a_new_version_and_leaves_the_old_one_alone(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """D1 is not bent by this: an edit is a create with less typing."""
    _, conn = home
    first = store(home)

    second = edit(home, '"to": ctx.inputs["to"]', '"who": ctx.inputs["to"]')
    assert second.ok, [str(e) for e in second.errors]
    assert second.created
    assert second.version != first.version
    assert second.workflow_id == first.workflow_id

    assert code_of(conn) == BALANCE.replace('"to": ctx.inputs', '"who": ctx.inputs')
    assert code_of(conn, version=first.version) == BALANCE
    assert conn.execute("SELECT count(*) AS n FROM workflow_versions").fetchone()["n"] == 2


def test_the_edited_code_goes_through_the_compiler(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """An edit that breaks the workflow is rejected like any other bad code.

    Nothing is stored, so the workflow that worked five seconds ago still works.
    """
    _, conn = home
    first = store(home)
    result = edit(home, "ctx.pennylane.get_balance()", "ctx.pennylane.nope()")

    assert result.ok is False
    assert result.stage == "typecheck"
    assert result.errors[0].line == 5
    assert result.errors[0].hint

    assert code_of(conn) == BALANCE
    assert conn.execute("SELECT count(*) AS n FROM workflow_versions").fetchone()["n"] == 1
    assert first.version is not None


def test_the_schemas_and_the_description_carry_over(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """An edit is about the code. Re-declaring the contract to change a line
    would be exactly the round trip this tool exists to avoid."""
    _, conn = home
    store(
        home,
        BALANCE.replace("-> dict[str, object]", "-> Output").replace(
            "from runlace_types import Ctx", "from runlace_types import Ctx, Output"
        ),
        outputs_schema=OUTPUTS,
    )

    result = edit(home, "ctx.pennylane.get_balance()", "ctx.pennylane.get_balance()\n    pass")
    assert result.ok, [str(e) for e in result.errors]

    record = get_workflow(conn, "report")
    assert record is not None
    assert record["inputs_schema"] == INPUTS
    assert record["outputs_schema"] == OUTPUTS
    assert record["description"] == "A report."


def test_a_schema_passed_to_an_edit_replaces_the_inherited_one(
    home: tuple[RunlacePaths, Connection]
) -> None:
    _, conn = home
    store(home)
    wider = {"type": "object", "properties": {"to": {"type": "string"}}}

    result = edit(
        home,
        '"to": ctx.inputs["to"]',
        '"to": ctx.inputs.get("to")',
        description="A wider report.",
        inputs_schema=wider,
    )
    assert result.ok, [str(e) for e in result.errors]

    record = get_workflow(conn, "report")
    assert record is not None
    assert record["inputs_schema"] == wider
    assert record["description"] == "A wider report."


def test_an_edit_re_extracts_the_tools_it_uses(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """The pinned tools come from the new source, not from the version edited.

    Otherwise an edit could add a side effect that the confirm gate never sees.
    """
    store(home)
    result = edit(
        home,
        "    ctx.pennylane.get_balance()\n",
        "    ctx.pennylane.get_balance()\n"
        '    ctx.gmail.send_email(to=ctx.inputs["to"], subject="s", body="b")\n',
    )
    assert result.ok, [str(e) for e in result.errors]
    assert {(t["connector"], t["tool"]) for t in result.tools_used} == {
        ("pennylane", "get_balance"),
        ("gmail", "send_email"),
    }
    assert any("confirm=True" in w for w in result.warnings)


# -- the refusals ----------------------------------------------------------


def test_a_string_that_appears_twice_is_refused_rather_than_guessed(
    home: tuple[RunlacePaths, Connection]
) -> None:
    _, conn = home
    store(home, TWICE)
    result = edit(home, "    ctx.pennylane.get_balance()\n", "    pass\n")

    assert result.ok is False
    assert result.stage == STAGE_EDIT
    assert result.errors[0].code == "not-unique"
    assert "appears 2 times" in result.errors[0].message
    assert "surrounding lines" in result.errors[0].hint
    assert code_of(conn) == TWICE


def test_a_string_that_appears_nowhere_says_how_to_get_it_right(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """The likely cause is retyped indentation, so the hint says to copy it."""
    store(home)
    result = edit(home, "ctx.pennylane.get_ballance()", "pass")

    assert result.errors[0].code == "no-match"
    assert "exactly" in result.errors[0].hint
    assert "get_workflow" in result.errors[0].hint


def test_an_edit_that_changes_nothing_is_refused(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """Otherwise it compiles, matches the existing hash, and reports success --
    a model reading that would think its fix landed."""
    store(home)
    result = edit(home, "ctx.pennylane.get_balance()", "ctx.pennylane.get_balance()")
    assert result.errors[0].code == "no-change"


def test_editing_a_workflow_that_does_not_exist_says_what_to_do(
    home: tuple[RunlacePaths, Connection]
) -> None:
    result = edit(home, "a", "b", name="nope")
    assert result.errors[0].code == "unknown-workflow"
    assert "list_workflows" in result.errors[0].hint
    assert "create_workflow" in result.errors[0].hint


def test_an_edit_whose_file_vanished_from_disk_says_where_it_looked(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """There is no code to apply the replacement to, and that is worth saying
    plainly rather than reporting it as a string that failed to match."""
    paths, conn = home
    created = store(home)
    (paths.workflows / "report" / f"{created.version}.py").unlink()

    result = edit(home, "ctx.pennylane.get_balance()", "pass")
    assert result.errors[0].code == "missing-code"
    assert "create_workflow" in result.errors[0].hint
    assert conn.execute("SELECT count(*) AS n FROM workflow_versions").fetchone()["n"] == 1
