"""Storing workflows: immutable versions, files on disk, drift status (D1)."""

from __future__ import annotations

import json

from runlace.db import Connection
from runlace.paths import RunlacePaths
from runlace.workflows import create_workflow, get_workflow, list_workflows

INPUTS = {
    "type": "object",
    "properties": {"to": {"type": "string"}},
    "required": ["to"],
}


def code_calling(*tools: str, extra: str = "") -> str:
    body = "\n".join(f"    ctx.{t}" for t in tools) or "    pass"
    return (
        "from runlace_types import Ctx\n\n\n"
        "def run(ctx: Ctx) -> dict[str, object]:\n"
        f"{body}\n"
        f'    return {{"to": ctx.inputs["to"]{extra}}}\n'
    )


BALANCE = code_calling("pennylane.get_balance()")
BALANCE_TWICE = code_calling("pennylane.get_balance()", "pennylane.get_balance()")
SENDS_EMAIL = code_calling(
    'gmail.send_email(to=ctx.inputs["to"], subject="s", body="b")'
)


def test_creating_writes_the_file_and_the_rows(
    home: tuple[RunlacePaths, Connection]
) -> None:
    paths, conn = home
    result = create_workflow(
        conn, paths, name="report", description="A report.", code=BALANCE,
        inputs_schema=INPUTS,
    )
    assert result.ok, [str(e) for e in result.errors]
    assert result.workflow_id is not None and result.workflow_id.startswith("wf_")
    assert result.created

    file_path = paths.workflows / "report" / f"{result.version}.py"
    assert file_path.read_text() == BALANCE

    row = conn.execute("SELECT * FROM workflows").fetchone()
    assert row["name"] == "report"
    assert row["latest_version"] == result.version_id
    version = conn.execute("SELECT * FROM workflow_versions").fetchone()
    assert version["file_path"] == str(file_path)
    assert json.loads(version["tools_used_json"])[0]["tool"] == "get_balance"


def test_creating_twice_under_one_name_yields_two_versions(
    home: tuple[RunlacePaths, Connection]
) -> None:
    paths, conn = home
    first = create_workflow(
        conn, paths, name="report", description="v1", code=BALANCE,
        inputs_schema=INPUTS,
    )
    second = create_workflow(
        conn, paths, name="report", description="v2", code=BALANCE_TWICE,
        inputs_schema=INPUTS,
    )
    assert first.ok and second.ok
    assert first.workflow_id == second.workflow_id
    assert first.version != second.version

    versions = conn.execute(
        "SELECT version_hash FROM workflow_versions ORDER BY created_at, id"
    ).fetchall()
    assert {str(v["version_hash"]) for v in versions} == {first.version, second.version}
    assert len(versions) == 2


def test_the_earlier_version_is_never_overwritten(
    home: tuple[RunlacePaths, Connection]
) -> None:
    paths, conn = home
    first = create_workflow(
        conn, paths, name="report", description="v1", code=BALANCE,
        inputs_schema=INPUTS,
    )
    create_workflow(
        conn, paths, name="report", description="v2", code=BALANCE_TWICE,
        inputs_schema=INPUTS,
    )
    assert (paths.workflows / "report" / f"{first.version}.py").read_text() == BALANCE
    latest = conn.execute("SELECT latest_version FROM workflows").fetchone()
    assert latest["latest_version"] != first.version_id


def test_resubmitting_identical_code_reuses_the_version(
    home: tuple[RunlacePaths, Connection]
) -> None:
    """Versions are content-addressed, so the same content is the same version."""
    paths, conn = home
    first = create_workflow(
        conn, paths, name="report", description="d", code=BALANCE, inputs_schema=INPUTS
    )
    again = create_workflow(
        conn, paths, name="report", description="d", code=BALANCE, inputs_schema=INPUTS
    )
    assert again.ok and not again.created
    assert again.version == first.version
    assert conn.execute("SELECT COUNT(*) AS n FROM workflow_versions").fetchone()["n"] == 1
    assert any("no new version" in w for w in again.warnings)


def test_changing_only_the_outputs_schema_is_a_new_version(
    home: tuple[RunlacePaths, Connection]
) -> None:
    paths, conn = home
    code = (
        "from runlace_types import Ctx, Output\n\n\n"
        "def run(ctx: Ctx) -> Output:\n"
        '    return {"to": ctx.inputs["to"]}\n'
    )
    first = create_workflow(
        conn, paths, name="report", description="d", code=code, inputs_schema=INPUTS,
        outputs_schema={"type": "object", "properties": {"to": {"type": "string"}}},
    )
    second = create_workflow(
        conn, paths, name="report", description="d", code=code, inputs_schema=INPUTS,
        outputs_schema={
            "type": "object",
            "properties": {"to": {"type": "string"}},
            "required": ["to"],
        },
    )
    assert first.ok and second.ok, [str(e) for e in first.errors + second.errors]
    assert first.version != second.version


def test_a_workflow_that_fails_to_compile_is_not_stored(
    home: tuple[RunlacePaths, Connection]
) -> None:
    paths, conn = home
    result = create_workflow(
        conn, paths, name="report", description="d",
        code="import os\n\n\ndef run(ctx) -> dict[str, object]:\n    return {}\n",
        inputs_schema=INPUTS,
    )
    assert not result.ok
    assert result.stage == "lint"
    assert conn.execute("SELECT COUNT(*) AS n FROM workflows").fetchone()["n"] == 0
    assert list(paths.workflows.iterdir()) == []


def test_a_bad_name_is_rejected_before_anything_is_compiled(
    home: tuple[RunlacePaths, Connection]
) -> None:
    paths, conn = home
    result = create_workflow(
        conn, paths, name="../escape", description="d", code=BALANCE,
        inputs_schema=INPUTS,
    )
    assert not result.ok
    assert result.stage == "validate"
    assert [e.code for e in result.errors] == ["bad-name"]


def test_side_effects_are_reported_as_a_warning(
    home: tuple[RunlacePaths, Connection]
) -> None:
    paths, conn = home
    result = create_workflow(
        conn, paths, name="notify", description="d", code=SENDS_EMAIL,
        inputs_schema=INPUTS,
    )
    assert result.ok, [str(e) for e in result.errors]
    assert result.tools_used[0]["risk"] == "side_effect"
    assert any("confirm=True" in w for w in result.warnings)


# -- reading ---------------------------------------------------------------


def test_list_workflows_reports_the_latest_version(
    home: tuple[RunlacePaths, Connection]
) -> None:
    paths, conn = home
    create_workflow(
        conn, paths, name="report", description="A report.", code=BALANCE,
        inputs_schema=INPUTS,
    )
    second = create_workflow(
        conn, paths, name="report", description="A report.", code=BALANCE_TWICE,
        inputs_schema=INPUTS,
    )
    create_workflow(
        conn, paths, name="notify", description="Sends mail.", code=SENDS_EMAIL,
        inputs_schema=INPUTS,
    )
    listed = list_workflows(conn)
    assert [w["name"] for w in listed] == ["notify", "report"]
    report = next(w for w in listed if w["name"] == "report")
    assert report["latest_version"] == second.version
    assert report["versions"] == 2
    assert report["last_run"] is None  # nothing has run: that is M3


def test_get_workflow_returns_the_code_and_the_pinned_tools(
    home: tuple[RunlacePaths, Connection]
) -> None:
    paths, conn = home
    created = create_workflow(
        conn, paths, name="report", description="A report.", code=BALANCE,
        inputs_schema=INPUTS,
    )
    record = get_workflow(conn, "report")
    assert record is not None
    assert record["code"] == BALANCE
    assert record["version"] == created.version
    assert record["is_latest"]
    assert record["inputs_schema"] == INPUTS
    assert record["outputs_schema"] is None
    assert [t["tool"] for t in record["tools_used"]] == ["get_balance"]
    assert record["drift"]["ok"]


def test_get_workflow_accepts_an_id_and_an_older_version(
    home: tuple[RunlacePaths, Connection]
) -> None:
    paths, conn = home
    first = create_workflow(
        conn, paths, name="report", description="d", code=BALANCE, inputs_schema=INPUTS
    )
    create_workflow(
        conn, paths, name="report", description="d", code=BALANCE_TWICE,
        inputs_schema=INPUTS,
    )
    assert first.workflow_id is not None
    by_id = get_workflow(conn, first.workflow_id)
    assert by_id is not None and by_id["code"] == BALANCE_TWICE

    older = get_workflow(conn, "report", version=str(first.version))
    assert older is not None
    assert older["code"] == BALANCE
    assert not older["is_latest"]
    assert [v["version"] for v in older["versions"]] == [
        first.version,
        by_id["version"],
    ]


def test_get_workflow_is_none_for_an_unknown_name(
    home: tuple[RunlacePaths, Connection]
) -> None:
    _, conn = home
    assert get_workflow(conn, "nope") is None


def test_drift_is_reported_when_a_pinned_schema_changes(
    home: tuple[RunlacePaths, Connection]
) -> None:
    paths, conn = home
    create_workflow(
        conn, paths, name="report", description="d", code=BALANCE, inputs_schema=INPUTS
    )
    conn.execute(
        "UPDATE tools SET schema_hash = 'changed' WHERE name = 'get_balance'"
    )
    conn.commit()

    record = get_workflow(conn, "report")
    assert record is not None
    drift = record["drift"]
    assert not drift["ok"]
    assert drift["changed"] == [
        {
            "connector": "pennylane",
            "tool": "get_balance",
            "pinned": record["tools_used"][0]["schema_hash"],
            "current": "changed",
        }
    ]


def test_drift_reports_a_tool_that_disappeared(
    home: tuple[RunlacePaths, Connection]
) -> None:
    paths, conn = home
    create_workflow(
        conn, paths, name="report", description="d", code=BALANCE, inputs_schema=INPUTS
    )
    conn.execute("DELETE FROM tools WHERE name = 'get_balance'")
    conn.commit()

    record = get_workflow(conn, "report")
    assert record is not None
    assert record["drift"]["missing"] == [
        {"connector": "pennylane", "tool": "get_balance"}
    ]
