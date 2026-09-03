"""Executing a stored workflow: the gates, the run, the journal.

The order is the spec's, and it is the whole point of this module:

1. validate ``inputs`` against the declared schema (D7);
2. refuse if any pinned tool's schema has drifted since the workflow compiled;
3. refuse if the workflow has side effects and ``confirm`` was not passed (D6);
4. run it in the subprocess, journaling every tool call as it happens;
5. validate the return value against ``outputs_schema`` if there is one.

Every attempt that got as far as naming a version is journaled, refusals
included -- ``runs`` and ``steps`` are the audit log, not a success log.
"""

from __future__ import annotations

import uuid
from typing import Any

from .config import Connector, read_config
from .db import (
    Connection,
    finish_run,
    insert_run,
    insert_step,
    tool_output_schemas,
)
from .paths import RunlacePaths
from .policy import Policy, read_policy
from .runner import (
    DEFAULT_TIMEOUT_SECONDS,
    STATUS_COMPLETED,
    STATUS_FAILED,
    CallTool,
    Step,
    run_code,
)
from .sessions import open_sessions
from .simulate import stand_in
from .validation import FieldError, apply_defaults, validate
from .workflows import get_workflow

CODE_UNKNOWN_WORKFLOW = "unknown-workflow"
CODE_NO_CODE = "missing-code"
CODE_INVALID_INPUTS = "invalid-inputs"
CODE_SCHEMA_DRIFT = "schema-drift"
CODE_NEEDS_CONFIRMATION = "needs-confirmation"
CODE_UNKNOWN_CONNECTOR = "unknown-connector"
CODE_CONNECT_FAILED = "connector-unreachable"
CODE_WORKFLOW_FAILED = "workflow-failed"
CODE_INVALID_OUTPUT = "invalid-output"


async def run_workflow(
    conn: Connection,
    paths: RunlacePaths,
    *,
    workflow: str,
    inputs: dict[str, Any] | None = None,
    confirm: bool = False,
    version: str | None = None,
    dry_run: bool = False,
    call_tool: CallTool | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run one stored workflow. Returns a result; it does not raise for failures.

    ``dry_run`` executes the workflow for real -- every read hits the live
    server, every branch is taken, the return value is validated -- but answers
    each side-effecting call from the tool's own ``outputSchema`` instead of
    letting it act. Nothing leaves the machine, so there is no confirmation gate
    on it; every other gate still applies.

    ``call_tool`` is injectable so tests can run a real subprocess against fake
    tools. Left unset, sessions are opened to the connectors the workflow uses.
    """
    record = get_workflow(conn, workflow, version=version)
    if record is None:
        return _refused(
            None,
            CODE_UNKNOWN_WORKFLOW,
            f"no workflow called `{workflow}`",
            "Call list_workflows to see what exists.",
        )
    if record.get("version") is None:
        return _refused(
            None,
            CODE_UNKNOWN_WORKFLOW,
            str(record.get("error") or "that workflow has no such version"),
            "Call get_workflow to see which versions exist.",
        )
    code = record.get("code")
    if not isinstance(code, str):
        return _refused(
            None,
            CODE_NO_CODE,
            f"the code for version {record['version']} is missing from disk",
            f"Expected it at {record.get('file_path')}. Re-create the workflow "
            f"with create_workflow.",
        )

    given = dict(inputs or {})
    resolved = apply_defaults(record.get("inputs_schema"), given)

    run_id = f"run_{uuid.uuid4().hex[:12]}"
    insert_run(
        conn,
        run_id=run_id,
        workflow_version_id=str(record["version_id"]),
        inputs=resolved,
        confirmed=confirm,
        dry_run=dry_run,
    )
    conn.commit()

    def refuse(code: str, error: str, hint: str, **extra: Any) -> dict[str, Any]:
        finish_run(conn, run_id, status=STATUS_FAILED, error=error)
        conn.commit()
        return _refused(
            run_id, code, error, hint, record=record, dry_run=dry_run, **extra
        )

    # 1. inputs
    input_errors = validate(record.get("inputs_schema"), resolved)
    if input_errors:
        return refuse(
            CODE_INVALID_INPUTS,
            _summarise(input_errors, "the inputs do not match the declared inputs_schema"),
            "Fix the listed fields and call run_workflow again. `get_workflow` "
            "returns the inputs_schema this workflow declares.",
            errors=[e.to_json() for e in input_errors],
        )

    # 2. drift
    drift = record.get("drift") or {}
    if not drift.get("ok", True):
        return refuse(
            CODE_SCHEMA_DRIFT,
            _drift_message(drift),
            "The tools this workflow was compiled against have changed. Run "
            "`runlace sync`, review what changed with get_workflow, and create a "
            "new version if the workflow still makes sense.",
            drift=drift,
        )

    # 3. confirm (D6)
    #
    # The risk pinned on the version is what the tool was when the workflow was
    # compiled. `policy.yaml` may have been edited since, and an edit that marks
    # a tool dangerous has to reach workflows that already exist -- otherwise
    # the override protects nothing you already built. It only ever tightens:
    # relaxing a pinned `side_effect` would need a new version, which is the
    # safe direction to require paperwork in.
    policy = read_policy(paths.policy)
    side_effects = [
        t
        for t in record["tools_used"]
        if _effective_risk(policy, t) != "read_only"
    ]
    if side_effects and not confirm and not dry_run:
        names = ", ".join(f"{t['connector']}.{t['tool']}" for t in side_effects)
        return refuse(
            CODE_NEEDS_CONFIRMATION,
            f"this workflow performs side effects ({names}) and was called "
            f"without confirm",
            "Show the human exactly which tools will act, and call run_workflow "
            "again with confirm=True only after they agree.",
            side_effects=[
                {"connector": t["connector"], "tool": t["tool"]} for t in side_effects
            ],
        )

    # 4. run
    #
    # In a dry run the side-effecting tools are never called, so the servers
    # that only host them are never needed either -- you can dry-run a workflow
    # before the connector that would send the email is even reachable.
    simulated = [
        {"connector": str(t["connector"]), "tool": str(t["tool"])} for t in side_effects
    ]
    needed = [
        t
        for t in record["tools_used"]
        if not dry_run
        or {"connector": str(t["connector"]), "tool": str(t["tool"])} not in simulated
    ]
    connectors, missing = _connectors_for(paths, needed)
    if missing:
        return refuse(
            CODE_UNKNOWN_CONNECTOR,
            f"connector(s) {', '.join(missing)} are no longer configured",
            "Run `runlace init` to reconnect them, or create a version of this "
            "workflow that does not use them.",
        )

    def journal(step: Step) -> None:
        insert_step(
            conn,
            step_id=f"st_{uuid.uuid4().hex[:12]}",
            run_id=run_id,
            seq=step.seq,
            connector=step.connector,
            tool=step.tool,
            risk=step.risk,
            payload=step.payload,
            result=step.result,
            status=step.status,
            duration_ms=step.duration_ms,
            error=step.error,
        )
        conn.commit()

    stand_ins = _stand_ins(conn, simulated) if dry_run else {}

    if call_tool is not None:
        outcome = await run_code(
            conn,
            code=code,
            inputs=resolved,
            call_tool=_without_side_effects(call_tool, stand_ins),
            on_step=journal,
            timeout=timeout,
        )
    else:
        try:
            async with open_sessions(connectors) as sessions:
                outcome = await run_code(
                    conn,
                    code=code,
                    inputs=resolved,
                    call_tool=_without_side_effects(sessions.call, stand_ins),
                    on_step=journal,
                    timeout=timeout,
                )
        except Exception as exc:  # noqa: BLE001 - a server that will not talk is a run failure
            return refuse(
                CODE_CONNECT_FAILED,
                f"could not connect to this workflow's MCP servers "
                f"({type(exc).__name__}: {exc})",
                "Check the servers are running, then run `runlace init` to "
                "refresh what Runlace knows about them.",
            )

    steps = [s.to_json() for s in outcome.steps]
    # Every result from here on says whether it was a dry run and, if it was,
    # exactly which calls were answered rather than made. A caller must never
    # have to infer that from the risk column.
    kind: dict[str, Any] = (
        {"dry_run": True, "simulated": simulated} if dry_run else {"dry_run": False}
    )

    if not outcome.ok:
        error = outcome.message or "the workflow failed"
        finish_run(conn, run_id, status=STATUS_FAILED, error=error)
        conn.commit()
        return {
            **_identity(run_id, record),
            **kind,
            "ok": False,
            "status": STATUS_FAILED,
            "code": CODE_WORKFLOW_FAILED,
            "error": error,
            "detail": outcome.error,
            "hint": "The workflow raised while running. The line number in "
            "`detail` is a line of its own code; fix it and create a new version.",
            "output": None,
            "steps": steps,
        }

    # 5. output
    output_errors = validate(record.get("outputs_schema"), outcome.output)
    if output_errors:
        error = _summarise(
            output_errors,
            "the workflow's return value does not match the declared outputs_schema",
        )
        finish_run(conn, run_id, status=STATUS_FAILED, output=outcome.output, error=error)
        conn.commit()
        return {
            **_identity(run_id, record),
            **kind,
            "ok": False,
            "status": STATUS_FAILED,
            "code": CODE_INVALID_OUTPUT,
            "error": error,
            "errors": [e.to_json() for e in output_errors],
            "hint": _output_hint(dry_run),
            "output": outcome.output,
            "steps": steps,
        }

    finish_run(conn, run_id, status=STATUS_COMPLETED, output=outcome.output)
    conn.commit()
    return {
        **_identity(run_id, record),
        **kind,
        "ok": True,
        "status": STATUS_COMPLETED,
        "output": outcome.output,
        "steps": steps,
    }


# -- the dry run -----------------------------------------------------------


def _stand_ins(
    conn: Connection, simulated: list[dict[str, Any]]
) -> dict[tuple[str, str], Any]:
    """One stand-in value per tool a dry run will not call, built up front."""
    schemas = tool_output_schemas(conn)
    return {
        (t["connector"], t["tool"]): stand_in(schemas.get((t["connector"], t["tool"])))
        for t in simulated
    }


def _without_side_effects(
    call_tool: CallTool, stand_ins: dict[tuple[str, str], Any]
) -> CallTool:
    """``call_tool``, with the listed tools answered instead of called.

    Empty in a real run, so both paths go through the same wrapper and there is
    only one place where a tool call happens.
    """
    if not stand_ins:
        return call_tool

    async def call(connector: str, tool: str, payload: dict[str, Any]) -> Any:
        if (connector, tool) in stand_ins:
            return stand_ins[(connector, tool)]
        return await call_tool(connector, tool, payload)

    return call


# -- helpers ---------------------------------------------------------------


def _identity(run_id: str | None, record: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "workflow_id": record.get("workflow_id"),
        "name": record.get("name"),
        "version": record.get("version"),
    }


def _refused(
    run_id: str | None,
    code: str,
    error: str,
    hint: str,
    *,
    record: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """A run that never executed. Still a `failed` run when there is a row for it."""
    result: dict[str, Any] = {
        **(_identity(run_id, record) if record else {"run_id": run_id}),
        "ok": False,
        "status": STATUS_FAILED,
        "code": code,
        "error": error,
        "hint": hint,
        "output": None,
        "steps": [],
    }
    result.update(extra)
    return result


def _output_hint(dry_run: bool) -> str:
    """The same defect, but only one of the two readings has already cost you."""
    if dry_run:
        return (
            "The workflow ran to the end and returned the wrong shape. Nothing "
            "acted -- this is what the dry run is for. Fix the code or the "
            "outputs_schema and edit the workflow."
        )
    return (
        "The workflow ran, and its side effects happened, but what it returned "
        "does not match the outputs_schema it declares. Fix one or the other "
        "and create a new version."
    )


def _connectors_for(
    paths: RunlacePaths, tools_used: list[dict[str, Any]]
) -> tuple[list[Connector], list[str]]:
    """The configured connectors this workflow needs, and any that have gone."""
    wanted = sorted({str(t["connector"]) for t in tools_used})
    if not wanted:
        return [], []
    configured = {c.name: c for c in read_config(paths.config)}
    found = [configured[name] for name in wanted if name in configured]
    missing = [name for name in wanted if name not in configured]
    return found, missing


def _summarise(errors: list[FieldError], prefix: str) -> str:
    return f"{prefix}: " + "; ".join(str(e) for e in errors)


def _effective_risk(policy: Policy, pinned: dict[str, Any]) -> str:
    """The stricter of what was pinned and what `policy.yaml` says now."""
    if pinned.get("risk") != "read_only":
        return "side_effect"
    return policy.risk_for(
        str(pinned.get("connector", "")), str(pinned.get("tool", "")), "read_only"
    )


def _drift_message(drift: dict[str, Any]) -> str:
    parts: list[str] = []
    for tool in drift.get("changed", []):
        parts.append(f"{tool['connector']}.{tool['tool']} changed its schema")
    for tool in drift.get("missing", []):
        parts.append(f"{tool['connector']}.{tool['tool']} no longer exists")
    return "refusing to run: " + "; ".join(parts)
