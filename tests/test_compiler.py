"""The D3 pipeline end to end, against a populated Runlace home.

These tests shell out to pyright, so they are the slow ones in the suite. They
are also the ones that prove the milestone: an unknown tool or a wrong keyword
has to fail at typecheck, with a line number.
"""

from __future__ import annotations

from runlace.compiler import (
    STAGE_EXTRACT,
    STAGE_LINT,
    STAGE_TYPECHECK,
    _without_cascades,
    compile_workflow,
)
from runlace.db import Connection
from runlace.model import Model
from runlace.paths import RunlacePaths
from runlace.typecheck import Diagnostic

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


ROWS_SCHEMA = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        }
    },
    "required": ["rows"],
}


def test_a_list_of_objects_built_in_a_variable_gets_the_variance_hint(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """The schema is right and the value is right; only where it was built is wrong.

    A blind-tested model hit this twice. `rows = [...]` is inferred as
    `list[dict[str, str]]`, and a list of TypedDicts is not that, because lists
    are invariant. "Fix one or the other so they agree" sends the agent
    rewriting a schema that was never the problem.
    """
    paths, conn = home
    code = (
        "from runlace_types import Ctx, Output\n\n\n"
        "def run(ctx: Ctx) -> Output:\n"
        '    result = ctx.pennylane.list_transactions(from_="a", to="b")\n'
        '    rows = [{"id": t["id"]} for t in result["transactions"]]\n'
        '    return {"rows": rows}\n'
    )
    result = compile_workflow(
        conn, paths.types, name="probe", code=code, inputs_schema=None,
        outputs_schema=ROWS_SCHEMA,
    )
    assert not result.ok
    assert "inside the `return`" in result.errors[0].hint


def test_the_same_list_built_inside_the_return_compiles(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """Which is what makes the hint above worth giving."""
    paths, conn = home
    code = (
        "from runlace_types import Ctx, Output\n\n\n"
        "def run(ctx: Ctx) -> Output:\n"
        '    result = ctx.pennylane.list_transactions(from_="a", to="b")\n'
        '    return {"rows": [{"id": t["id"]} for t in result["transactions"]]}\n'
    )
    result = compile_workflow(
        conn, paths.types, name="probe", code=code, inputs_schema=None,
        outputs_schema=ROWS_SCHEMA,
    )
    assert result.ok, [str(e) for e in result.errors]


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


def test_the_lint_hint_knows_which_connector_a_tool_lives_on(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """`ctx.get_balance()` is what a model writes after reading a list of tools.

    Lint alone can only say "write `ctx.get_balance.<tool>(...)`", which is the
    same mistake one level deeper. Compiled against a real home it has the
    connector index, so it can name the server instead.
    """
    paths, conn = home
    code = (
        "from runlace_types import Ctx\n\n\n"
        "def run(ctx: Ctx) -> dict[str, object]:\n"
        "    ctx.get_balance()\n"
        '    return {"ok": True}\n'
    )
    result = compile_workflow(
        conn, paths.types, name="probe", code=code, inputs_schema=None
    )
    assert result.stage == STAGE_LINT
    assert result.errors[0].code == "unknown-connector"
    assert "ctx.pennylane.get_balance(...)" in result.errors[0].hint


# -- the cascade filter ------------------------------------------------------


def diagnostic(line: int, message: str, rule: str) -> Diagnostic:
    return Diagnostic(file="w.py", line=line, message=message, rule=rule)


def test_unknown_type_noise_is_dropped_from_every_line_not_just_the_broken_one() -> None:
    """A misspelled connector on line 9 makes lines 10 and 13 unknown too.

    Filtering line by line left those two in, and an 8B model that fixes the
    line it was shown never reaches the cause.
    """
    kept = _without_cascades(
        [
            diagnostic(9, 'Cannot access attribute "everthing"', "reportAttributeAccessIssue"),
            diagnostic(10, 'Type of "temperature" is unknown', "reportUnknownVariableType"),
            diagnostic(13, 'Type of "alerted" is unknown', "reportUnknownVariableType"),
        ]
    )
    assert [d.line for d in kept] == [9]


def test_unknown_types_survive_when_they_are_the_whole_complaint() -> None:
    """No real error to explain them means this is the accumulator case."""
    diagnostics = [
        diagnostic(4, 'Type of "rows" is unknown', "reportUnknownVariableType"),
        diagnostic(6, 'Type of "append" is unknown', "reportUnknownMemberType"),
    ]
    assert _without_cascades(diagnostics) == diagnostics


# -- ctx.ai ----------------------------------------------------------------

LOCAL_MODEL = Model(base_url="http://localhost:11434/v1", model="qwen3:8b")

AI_WORKFLOW = """\
from runlace_types import Ctx


def run(ctx: Ctx) -> dict[str, object]:
    verdict = ctx.ai(
        system="You classify invoices.",
        user=str(ctx.inputs["email"]),
        schema={
            "type": "object",
            "properties": {"urgent": {"type": "boolean"}},
            "required": ["urgent"],
        },
    )
    return {"urgent": verdict["urgent"]}
"""


def test_a_workflow_that_calls_the_model_compiles_and_is_marked(
    home: tuple[RunlacePaths, Connection]
) -> None:
    paths, conn = home
    result = compile_workflow(
        conn,
        paths.types,
        name="probe",
        code=AI_WORKFLOW,
        inputs_schema=INPUTS,
        model=LOCAL_MODEL,
    )
    assert result.ok, [str(e) for e in result.errors]
    assert result.uses_ai
    # An AI step is not a tool: nothing in `tools_used` resolves to a connector
    # called `ai`, and a run would refuse to start if one did.
    assert result.tools_used == []


def test_a_workflow_without_ai_is_not_marked(
    home: tuple[RunlacePaths, Connection]
) -> None:
    paths, conn = home
    result = compile_workflow(
        conn, paths.types, name="probe", code=READ_ONLY, inputs_schema=INPUTS
    )
    assert result.ok, [str(e) for e in result.errors]
    assert not result.uses_ai


def test_calling_the_model_with_no_model_configured_is_refused(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """Better here than three minutes into a run that cannot finish."""
    paths, conn = home
    result = compile_workflow(
        conn, paths.types, name="probe", code=AI_WORKFLOW, inputs_schema=INPUTS
    )
    assert not result.ok
    assert result.stage == STAGE_EXTRACT
    assert [e.code for e in result.errors] == ["no-model-configured"]
    assert result.errors[0].line == 5
    assert "runlace model set" in (result.errors[0].hint or "")


def test_a_schema_that_constrains_nothing_is_refused(
    home: tuple[RunlacePaths, Connection]
) -> None:
    paths, conn = home
    code = (
        "from runlace_types import Ctx\n\n\n"
        "def run(ctx: Ctx) -> dict[str, object]:\n"
        '    return ctx.ai(system="s", user="u", schema={"type": "string"})\n'
    )
    result = compile_workflow(
        conn,
        paths.types,
        name="probe",
        code=code,
        inputs_schema=INPUTS,
        model=LOCAL_MODEL,
    )
    assert not result.ok
    assert [e.code for e in result.errors] == ["ai-schema-not-an-object"]


def test_without_a_schema_the_answer_is_a_string_at_typecheck(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """The two overloads are the whole point: no schema, no dict."""
    paths, conn = home
    code = (
        "from runlace_types import Ctx\n\n\n"
        "def run(ctx: Ctx) -> dict[str, object]:\n"
        '    answer = ctx.ai(system="s", user="u")\n'
        '    return {"urgent": answer["urgent"]}\n'
    )
    result = compile_workflow(
        conn,
        paths.types,
        name="probe",
        code=code,
        inputs_schema=INPUTS,
        model=LOCAL_MODEL,
    )
    assert not result.ok
    assert result.stage == STAGE_TYPECHECK
    assert [e.line for e in result.errors] == [6]
