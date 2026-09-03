"""The D3 pipeline end to end, against a populated Runlace home.

These tests shell out to pyright, so they are the slow ones in the suite. They
are also the ones that prove the milestone: an unknown tool or a wrong keyword
has to fail at typecheck, with a line number.
"""

from __future__ import annotations

from runlace.compiler import STAGE_EXTRACT, STAGE_LINT, STAGE_TYPECHECK, compile_workflow
from runlace.db import Connection
from runlace.paths import RunlacePaths

INPUTS = {
    "type": "object",
    "properties": {
        "from": {"type": "string"},
        "to": {"type": "string"},
        "email": {"type": "string"},
    },
    "required": ["from", "to", "email"],
}

READ_ONLY = """\
from runlace_types import Ctx


def run(ctx: Ctx) -> dict[str, object]:
    result = ctx.pennylane.list_transactions(
        from_=ctx.inputs["from"], to=ctx.inputs["to"]
    )
    return {"count": len(result["transactions"])}
"""

MULTI_CONNECTOR = """\
from runlace_types import Ctx


def run(ctx: Ctx) -> dict[str, object]:
    result = ctx.pennylane.list_transactions(
        from_=ctx.inputs["from"], to=ctx.inputs["to"]
    )
    total = sum(t["amount"] for t in result["transactions"])
    ctx.gmail.send_email(
        to=ctx.inputs["email"], subject="Summary", body=str(total)
    )
    return {"count": len(result["transactions"]), "total": total}
"""


def test_a_read_only_workflow_compiles(
    home: tuple[RunlacePaths, Connection]
) -> None:
    paths, conn = home
    result = compile_workflow(
        conn, paths.types, name="probe", code=READ_ONLY, inputs_schema=INPUTS
    )
    assert result.ok, [str(e) for e in result.errors]
    assert [(t.connector, t.tool, t.risk) for t in result.tools_used] == [
        ("pennylane", "list_transactions", "read_only")
    ]
    assert not result.has_side_effects


def test_tools_used_is_inferred_across_connectors(
    home: tuple[RunlacePaths, Connection]
) -> None:
    paths, conn = home
    result = compile_workflow(
        conn, paths.types, name="probe", code=MULTI_CONNECTOR, inputs_schema=INPUTS
    )
    assert result.ok, [str(e) for e in result.errors]
    assert [(t.connector, t.tool, t.risk) for t in result.tools_used] == [
        ("gmail", "send_email", "side_effect"),
        ("pennylane", "list_transactions", "read_only"),
    ]
    assert result.has_side_effects
    assert any("confirm=True" in w for w in result.warnings)


def test_each_tool_is_pinned_to_the_schema_hash_it_compiled_against(
    home: tuple[RunlacePaths, Connection]
) -> None:
    paths, conn = home
    result = compile_workflow(
        conn, paths.types, name="probe", code=MULTI_CONNECTOR, inputs_schema=INPUTS
    )
    stored = {
        (str(r["connector"]), str(r["name"])): str(r["schema_hash"])
        for r in conn.execute("SELECT connector, name, schema_hash FROM tools")
    }
    assert {(t.connector, t.tool): t.schema_hash for t in result.tools_used} == {
        ("gmail", "send_email"): stored[("gmail", "send_email")],
        ("pennylane", "list_transactions"): stored[("pennylane", "list_transactions")],
    }


def test_call_sites_are_recorded(home: tuple[RunlacePaths, Connection]) -> None:
    paths, conn = home
    result = compile_workflow(
        conn, paths.types, name="probe", code=MULTI_CONNECTOR, inputs_schema=INPUTS
    )
    lines = {t.tool: t.lines for t in result.tools_used}
    assert lines["list_transactions"] == (5,)
    assert lines["send_email"] == (9,)


def test_lint_runs_before_pyright(home: tuple[RunlacePaths, Connection]) -> None:
    """A file that fails both stages reports the lint error, not pyright's."""
    paths, conn = home
    code = "import os\n\nfrom runlace_types import Ctx\n\n\ndef run(ctx: Ctx) -> dict[str, object]:\n    return ctx.pennylane.nope()\n"
    result = compile_workflow(
        conn, paths.types, name="probe", code=code, inputs_schema=INPUTS
    )
    assert not result.ok
    assert result.stage == STAGE_LINT
    assert [e.code for e in result.errors] == ["forbidden-import"]


# -- the typecheck acceptance criteria -------------------------------------


def test_an_unknown_tool_fails_at_typecheck_with_a_line_number(
    home: tuple[RunlacePaths, Connection]
) -> None:
    paths, conn = home
    code = (
        "from runlace_types import Ctx\n\n\n"
        "def run(ctx: Ctx) -> dict[str, object]:\n"
        '    ctx.pennylane.list_invoices(month="2024-01")\n'
        "    return {}\n"
    )
    result = compile_workflow(
        conn, paths.types, name="probe", code=code, inputs_schema=INPUTS
    )
    assert not result.ok
    assert result.stage == STAGE_TYPECHECK
    assert [e.line for e in result.errors] == [5]
    assert "list_invoices" in result.errors[0].message
    assert result.errors[0].hint


def test_an_unknown_connector_fails_at_typecheck(
    home: tuple[RunlacePaths, Connection]
) -> None:
    paths, conn = home
    code = (
        "from runlace_types import Ctx\n\n\n"
        "def run(ctx: Ctx) -> dict[str, object]:\n"
        '    ctx.slack.post_message(text="hi")\n'
        "    return {}\n"
    )
    result = compile_workflow(
        conn, paths.types, name="probe", code=code, inputs_schema=INPUTS
    )
    assert not result.ok
    assert result.stage == STAGE_TYPECHECK
    assert all(e.line == 5 for e in result.errors)
    assert "slack" in " ".join(e.message for e in result.errors)


def test_a_wrong_keyword_fails_at_typecheck_with_a_line_number(
    home: tuple[RunlacePaths, Connection]
) -> None:
    paths, conn = home
    code = (
        "from runlace_types import Ctx\n\n\n"
        "def run(ctx: Ctx) -> dict[str, object]:\n"
        '    ctx.pennylane.list_transactions(start="a", to="b")\n'
        "    return {}\n"
    )
    result = compile_workflow(
        conn, paths.types, name="probe", code=code, inputs_schema=INPUTS
    )
    assert not result.ok
    assert result.stage == STAGE_TYPECHECK
    assert all(e.line == 5 for e in result.errors)
    assert "start" in " ".join(e.message for e in result.errors)


def test_a_wrong_argument_type_fails_at_typecheck(
    home: tuple[RunlacePaths, Connection]
) -> None:
    paths, conn = home
    code = (
        "from runlace_types import Ctx\n\n\n"
        "def run(ctx: Ctx) -> dict[str, object]:\n"
        "    ctx.pennylane.list_transactions(from_=1, to=2)\n"
        "    return {}\n"
    )
    result = compile_workflow(
        conn, paths.types, name="probe", code=code, inputs_schema=INPUTS
    )
    assert not result.ok
    assert result.stage == STAGE_TYPECHECK


def test_reading_an_undeclared_input_fails_at_typecheck(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """`ctx.inputs` is narrowed to the declared inputs_schema (D7)."""
    paths, conn = home
    code = (
        "from runlace_types import Ctx\n\n\n"
        "def run(ctx: Ctx) -> dict[str, object]:\n"
        '    return {"x": ctx.inputs["not_declared"]}\n'
    )
    result = compile_workflow(
        conn, paths.types, name="probe", code=code, inputs_schema=INPUTS
    )
    assert not result.ok
    assert result.stage == STAGE_TYPECHECK
    assert "not_declared" in " ".join(e.message for e in result.errors)
    assert "inputs_schema" in result.errors[0].hint


def test_a_return_value_that_contradicts_outputs_schema_fails(
    home: tuple[RunlacePaths, Connection]
) -> None:
    paths, conn = home
    code = (
        "from runlace_types import Ctx, Output\n\n\n"
        "def run(ctx: Ctx) -> Output:\n"
        '    return {"count": "not-an-int"}\n'
    )
    result = compile_workflow(
        conn,
        paths.types,
        name="probe",
        code=code,
        inputs_schema=INPUTS,
        outputs_schema={
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
        },
    )
    assert not result.ok
    assert result.stage == STAGE_TYPECHECK
    assert [e.line for e in result.errors] == [5]


def test_a_return_value_matching_outputs_schema_compiles(
    home: tuple[RunlacePaths, Connection]
) -> None:
    paths, conn = home
    code = (
        "from runlace_types import Ctx, Output\n\n\n"
        "def run(ctx: Ctx) -> Output:\n"
        '    return {"count": 1}\n'
    )
    result = compile_workflow(
        conn,
        paths.types,
        name="probe",
        code=code,
        inputs_schema=INPUTS,
        outputs_schema={
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
        },
    )
    assert result.ok, [str(e) for e in result.errors]


def test_a_result_with_no_output_schema_is_usable_without_a_cast(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """`Any`, not `object`: a shape nobody promised is unknown, not opaque.

    This is a deliberate loss of a diagnostic. Under `object` the line below was
    a typecheck error -- but the fix it demanded was `cast(float, balance) * 2`,
    which pyright accepts on the author's word alone. The check never verified
    anything; it only charged a ritual for saying "trust me", and charged it on
    every call to a server like GitHub, where no tool declares an output schema.
    """
    paths, conn = home
    code = (
        "from runlace_types import Ctx\n\n\n"
        "def run(ctx: Ctx) -> dict[str, object]:\n"
        "    balance = ctx.pennylane.get_balance()\n"
        '    return {"doubled": balance * 2}\n'
    )
    result = compile_workflow(
        conn, paths.types, name="probe", code=code, inputs_schema=INPUTS
    )
    assert result.ok, [str(e) for e in result.errors]


def test_a_declared_output_schema_is_still_enforced(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """`Any` is confined to tools that promised nothing; it must not spread."""
    paths, conn = home
    code = (
        "from runlace_types import Ctx\n\n\n"
        "def run(ctx: Ctx) -> dict[str, object]:\n"
        "    rows = ctx.pennylane.list_transactions(from_='2024-01-01', to='2024-02-01')\n"
        '    return {"first": rows.no_such_attribute}\n'
    )
    result = compile_workflow(
        conn, paths.types, name="probe", code=code, inputs_schema=INPUTS
    )
    assert not result.ok
    assert result.stage == STAGE_TYPECHECK
    assert [e.line for e in result.errors] == [6]


# -- the extract stage as a backstop ---------------------------------------


def test_a_tool_missing_from_the_database_fails_at_extract(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """Stubs and database disagreeing is a Runlace bug, but it must not pass."""
    paths, conn = home
    conn.execute("DELETE FROM tools WHERE name = 'get_balance'")
    conn.commit()
    code = (
        "from runlace_types import Ctx\n\n\n"
        "def run(ctx: Ctx) -> dict[str, object]:\n"
        "    ctx.pennylane.get_balance()\n"
        "    return {}\n"
    )
    result = compile_workflow(
        conn, paths.types, name="probe", code=code, inputs_schema=INPUTS
    )
    assert not result.ok
    assert result.stage == STAGE_EXTRACT
    assert [e.code for e in result.errors] == ["unknown-tool"]
    assert result.errors[0].line == 5


def test_a_workflow_with_no_tool_calls_compiles_with_a_warning(
    home: tuple[RunlacePaths, Connection]
) -> None:
    paths, conn = home
    code = (
        "from runlace_types import Ctx\n\n\n"
        "def run(ctx: Ctx) -> dict[str, object]:\n"
        '    return {"echo": ctx.inputs["to"]}\n'
    )
    result = compile_workflow(
        conn, paths.types, name="probe", code=code, inputs_schema=INPUTS
    )
    assert result.ok, [str(e) for e in result.errors]
    assert result.tools_used == []
    assert any("calls no tools" in w for w in result.warnings)


def test_compiling_without_stubs_says_to_run_init(paths: RunlacePaths) -> None:
    from runlace.db import connect

    conn = connect(paths.db)
    try:
        result = compile_workflow(
            conn,
            paths.home / "missing_types",
            name="probe",
            code=READ_ONLY,
            inputs_schema=INPUTS,
        )
    finally:
        conn.close()
    assert not result.ok
    assert result.stage == STAGE_TYPECHECK
    assert "runlace init" in result.errors[0].hint


def test_compilation_leaves_nothing_behind(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """Compiling is a verdict, not a write: only `create_workflow` persists."""
    paths, conn = home
    before = sorted(p.name for p in paths.home.iterdir())
    compile_workflow(
        conn, paths.types, name="probe", code=MULTI_CONNECTOR, inputs_schema=INPUTS
    )
    assert sorted(p.name for p in paths.home.iterdir()) == before
    assert list(paths.workflows.iterdir()) == []
