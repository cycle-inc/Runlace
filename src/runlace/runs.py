"""Executing a stored workflow: the gates, the run, the journal.

The order is the spec's, and it is the whole point of this module:

1. validate ``inputs`` against the declared schema (D7);
2. refuse if any pinned tool's schema has drifted since the workflow compiled;
3. stop if the workflow has side effects and ``confirm`` was not passed (D6);
4. run it in the subprocess, journaling every tool call as it happens;
5. validate the return value against ``outputs_schema`` if there is one.

Step 3 stops in one of two ways, and which one depends on whether there is a
daemon to come back to. With one, the run is *parked*: it keeps its inputs and
waits for the developer's product to ask a human we cannot reach. Without one
there is nobody to resume it later, so it is refused the way v1 refused it --
tell the human in the conversation and call again with ``confirm``.

Every attempt that got as far as naming a version is journaled, refusals
included -- ``runs`` and ``steps`` are the audit log, not a success log.

Steps 1-3 are :func:`admit` and steps 4-5 are :func:`execute`, with
:func:`run_workflow` doing both back to back. The seam is there because the
queue needs it: the gates have to answer the caller straight away -- a
misspelled input is not news to break to someone three minutes later -- while
the running part is what a worker picks up whenever it gets to it.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .ai import Answer
from .ai import answer as ask_model
from .config import Connector, read_config
from .db import (
    Connection,
    enqueue_run,
    find_run,
    finish_run,
    insert_run,
    insert_step,
    park_run,
    tool_output_schemas,
)
from .journal import get_run as read_run
from .model import Model, read_model
from .paths import RunlacePaths
from .policy import Policy, read_policy
from .risk import READ_ONLY, SIDE_EFFECT, Risk
from .runner_shim import AI_CONNECTOR, AI_TOOL
from .queue import (
    APPROVAL_ASK,
    AWAITING_APPROVAL,
    CODE_AWAITING_APPROVAL,
    QUEUED,
    TERMINAL,
    has_live_worker,
)
from .runner import (
    DEFAULT_TIMEOUT_SECONDS,
    STATUS_COMPLETED,
    STATUS_FAILED,
    AiBridge,
    Ask,
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
CODE_NOT_FINISHED = "not-finished"

# Where the run executed. Not the caller's choice, so it has to be in the answer.
EXECUTED_INLINE = "inline"
EXECUTED_QUEUED = "queued"

# How often a caller waiting on a queued run looks to see whether it is over.
POLL_SECONDS = 0.02


@dataclass(frozen=True)
class Answered:
    """The gates settled it themselves; nothing is going to execute now.

    Two things end this way and they are not the same thing: a run that was
    turned away, and a run that is parked waiting for a human. Both leave
    :func:`admit` with the whole answer already shaped, which is all its caller
    needs to know -- what to tell whoever asked, and that there is nothing to
    hand to a worker.
    """

    result: dict[str, Any]


@dataclass(frozen=True)
class Admitted:
    """A run that passed the gates: everything :func:`execute` needs, and no more.

    It is deliberately not a database row. What the gates decided -- the
    resolved inputs after defaults, which tools count as side effects under
    *today's* policy -- is settled at admission time and must not be recomputed
    later against a policy file somebody edited in between.
    """

    run_id: str
    record: dict[str, Any]
    inputs: dict[str, Any]
    dry_run: bool = False
    side_effects: list[dict[str, Any]] = field(default_factory=list)


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
    ask: Ask | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    wait: float | None = None,
    approval: str = APPROVAL_ASK,
) -> dict[str, Any]:
    """Run one stored workflow. Returns a result; it does not raise for failures.

    ``dry_run`` executes the workflow for real -- every read hits the live
    server, every branch is taken, the return value is validated -- but answers
    each side-effecting call from the tool's own ``outputSchema`` instead of
    letting it act. Nothing leaves the machine, so there is no confirmation gate
    on it; every other gate still applies.

    ``wait`` is how long to stay with the run once it is handed to a worker.
    ``None`` waits for it to finish, which is what v1 did and still the right
    default: an MCP host is a conversation, and a two-second workflow answered
    in two seconds beats one the caller has to come back for. ``0`` returns as
    soon as the run is in the queue. Anything else waits that many seconds and
    then reports whatever state the run is in.

    Where the run actually executes is not the caller's choice, and the answer
    says which happened in ``executed``: ``queued`` when a daemon took it,
    ``inline`` when there was no daemon to take it and this process ran it
    itself. A difference in behaviour that depends on how the user launched the
    server is exactly the kind of thing that must be in the answer rather than
    in the docs.

    ``approval`` is the developer's, set where they launch Runlace, and never
    the agent's: ``ask`` parks a side-effecting run until a human answers,
    ``allow`` lets it through because their product got approval its own way.

    ``call_tool`` is injectable so tests can run a real subprocess against fake
    tools. Left unset, sessions are opened to the connectors the workflow uses;
    it also forces the inline path, because a worker in another process has no
    way to be handed a Python callable. ``ask`` is the same arrangement for
    `ctx.ai`, and forces the inline path for the same reason.
    """
    handed_over = call_tool is None and ask is None and has_live_worker(conn)
    admission = admit(
        conn,
        paths,
        workflow=workflow,
        inputs=inputs,
        confirm=confirm,
        version=version,
        dry_run=dry_run,
        queued=handed_over,
        approval=approval,
    )
    if isinstance(admission, Answered):
        return admission.result
    if not handed_over:
        result = await execute(
            conn, paths, admission, call_tool=call_tool, ask=ask, timeout=timeout
        )
        return {**result, "executed": EXECUTED_INLINE}
    return await _wait_for(conn, admission, wait)


def admit(
    conn: Connection,
    paths: RunlacePaths,
    *,
    workflow: str,
    inputs: dict[str, Any] | None = None,
    confirm: bool = False,
    version: str | None = None,
    dry_run: bool = False,
    queued: bool = False,
    approval: str = APPROVAL_ASK,
) -> Answered | Admitted:
    """The gates: everything that can be decided before anything executes.

    Writes the run row, so a refusal is journaled like any other attempt, and
    touches no MCP server -- this is cheap and synchronous on purpose.

    ``queued`` puts the run in the queue once it passes, for the caller that is
    about to hand it to a worker instead of executing it. It is also what makes
    parking possible at all: parking a run only means something if something
    will still be there to run it once a human answers.

    The row is opened as ``running`` whatever happens, and only joins the queue
    or the parked pile at the very end. A run that is claimable -- or
    approvable -- while the gates are still deciding could be picked up and
    executed a millisecond before being refused.
    """
    record = get_workflow(conn, workflow, version=version)
    if record is None:
        return Answered(
            _refused(
                None,
                CODE_UNKNOWN_WORKFLOW,
                f"no workflow called `{workflow}`",
                "Call list_workflows to see what exists.",
            )
        )
    if record.get("version") is None:
        return Answered(
            _refused(
                None,
                CODE_UNKNOWN_WORKFLOW,
                str(record.get("error") or "that workflow has no such version"),
                "Call get_workflow to see which versions exist.",
            )
        )
    if not isinstance(record.get("code"), str):
        return Answered(
            _refused(
                None,
                CODE_NO_CODE,
                f"the code for version {record['version']} is missing from disk",
                f"Expected it at {record.get('file_path')}. Re-create the workflow "
                f"with create_workflow.",
            )
        )

    given = dict(inputs or {})
    resolved = apply_defaults(record.get("inputs_schema"), given)
    # Worked out before the run row is written so that what the gates decided is
    # stored with the run rather than recomputed later against a policy file
    # somebody edited in between.
    side_effects = side_effects_of(
        read_policy(paths.policy), record, read_model(paths.model)
    )

    run_id = f"run_{uuid.uuid4().hex[:12]}"
    insert_run(
        conn,
        run_id=run_id,
        workflow_version_id=str(record["version_id"]),
        inputs=resolved,
        confirmed=confirm,
        dry_run=dry_run,
        side_effects=side_effects,
    )
    conn.commit()

    def refuse(code: str, error: str, hint: str, **extra: Any) -> Answered:
        return Answered(
            _fail(conn, run_id, record, code, error, hint, dry_run=dry_run, **extra)
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

    # 3. approval (D6)
    if side_effects and not confirm and not dry_run and approval == APPROVAL_ASK:
        if not queued:
            return refuse(
                CODE_NEEDS_CONFIRMATION,
                _acting(side_effects, "and was called without confirm"),
                "Show the human exactly which tools will act, and call "
                "run_workflow again with confirm=True only after they agree.",
                side_effects=side_effects,
            )
        park_run(conn, run_id)
        conn.commit()
        return Answered(_parked(run_id, record, resolved, side_effects, dry_run))

    if queued:
        enqueue_run(conn, run_id)
        conn.commit()
    return Admitted(
        run_id=run_id,
        record=record,
        inputs=resolved,
        dry_run=dry_run,
        side_effects=side_effects,
    )


async def execute(
    conn: Connection,
    paths: RunlacePaths,
    admitted: Admitted,
    *,
    call_tool: CallTool | None = None,
    ask: Ask | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run an admitted workflow to the end and journal it. Steps 4 and 5.

    Everything here needs the outside world -- a subprocess, the MCP servers --
    which is why it is separate from :func:`admit` and why a worker can be the
    one doing it, minutes after the caller was answered.
    """
    run_id = admitted.run_id
    record = admitted.record
    dry_run = admitted.dry_run
    resolved = admitted.inputs
    code = str(record["code"])

    def refuse(code: str, error: str, hint: str, **extra: Any) -> dict[str, Any]:
        return _fail(conn, run_id, record, code, error, hint, dry_run=dry_run, **extra)

    # 4. run
    #
    # In a dry run the side-effecting tools are never called, so the servers
    # that only host them are never needed either -- you can dry-run a workflow
    # before the connector that would send the email is even reachable.
    simulated = admitted.side_effects if dry_run else []
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
            tokens_in=step.tokens_in,
            tokens_out=step.tokens_out,
        )
        conn.commit()

    # `ai.complete` is in `simulated` when the gates called the model a side
    # effect, but it is not a tool and has no stand-in to look up in the
    # database: the bridge invents its answer from the schema instead.
    ai_acts = {"connector": AI_CONNECTOR, "tool": AI_TOOL} in admitted.side_effects
    stand_ins = _stand_ins(
        conn, [t for t in simulated if t["connector"] != AI_CONNECTOR]
    )
    ai = _ai_bridge(
        paths,
        ask,
        risk=SIDE_EFFECT if ai_acts else READ_ONLY,
        simulate=dry_run and ai_acts,
    )

    if call_tool is not None:
        outcome = await run_code(
            conn,
            code=code,
            inputs=resolved,
            call_tool=_without_side_effects(call_tool, stand_ins),
            ai=ai,
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
                    ai=ai,
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


async def _wait_for(
    conn: Connection, admitted: Admitted, wait: float | None
) -> dict[str, Any]:
    """Stay with a queued run for as long as the caller asked, then report.

    Polling, not a notification: the worker is often in another process, and a
    SELECT on an indexed primary key every fiftieth of a second is cheaper than
    anything that would let two processes signal each other.
    """
    deadline = None if wait is None else time.monotonic() + wait
    while deadline is None or time.monotonic() < deadline:
        row = find_run(conn, admitted.run_id)
        if row is not None and str(row["status"]) in TERMINAL:
            return {
                **read_run(conn, admitted.run_id),
                "executed": EXECUTED_QUEUED,
            }
        await asyncio.sleep(POLL_SECONDS)
    return _unfinished(conn, admitted)


def _acting(side_effects: list[dict[str, Any]], tail: str) -> str:
    names = ", ".join(f"{t['connector']}.{t['tool']}" for t in side_effects)
    return f"this workflow performs side effects ({names}) {tail}"


def _parked(
    run_id: str,
    record: dict[str, Any],
    inputs: dict[str, Any],
    side_effects: list[dict[str, Any]],
    dry_run: bool,
) -> dict[str, Any]:
    """A run set aside until a human answers. Everything needed to ask them.

    ``inputs`` are in the answer on purpose: the developer's product has to
    render *what* is about to happen, and it should render the resolved inputs
    -- defaults filled in, validated -- rather than the ones the agent typed.
    Those are also the ones stored, so what the human is shown is what runs.

    ``ok`` is false because nothing has happened yet. There is no failure here
    and the ``code`` says so, but a model reading ``ok: True`` would tell its
    user the email was sent.
    """
    return {
        **_identity(run_id, record),
        "ok": False,
        "code": CODE_AWAITING_APPROVAL,
        "status": AWAITING_APPROVAL,
        "dry_run": dry_run,
        "error": None,
        "side_effects": side_effects,
        "inputs": inputs,
        "hint": _acting(
            side_effects,
            "and is waiting for a human to approve it. Nothing has run. You "
            "cannot approve it yourself -- report this run_id to whoever asked "
            "and let them answer through the product they are using.",
        ),
        "output": None,
        "steps": [],
    }


def _unfinished(conn: Connection, admitted: Admitted) -> dict[str, Any]:
    """The answer for a run that is still going when the caller stops waiting.

    ``ok`` is false, and that is a judgement call worth defending: nothing has
    failed. But this result is read overwhelmingly by a model, and of the two
    ways to be wrong about it, "thought it was not done when it was" costs one
    extra call, while "thought it was done when it was not" makes the agent
    report a success that has not happened yet.
    """
    row = find_run(conn, admitted.run_id)
    status = str(row["status"]) if row is not None else QUEUED
    return {
        **_identity(admitted.run_id, admitted.record),
        "ok": False,
        "code": CODE_NOT_FINISHED,
        "status": status,
        "executed": EXECUTED_QUEUED,
        "dry_run": admitted.dry_run,
        "error": None,
        "hint": f"It is `{status}`. Nothing has been reported yet -- call "
        f"get_run with this run_id to see how it went.",
        "output": None,
        "steps": [],
    }


def _ai_bridge(
    paths: RunlacePaths,
    ask: Ask | None,
    *,
    risk: Risk = READ_ONLY,
    simulate: bool = False,
) -> AiBridge | None:
    """What `ctx.ai(...)` reaches during this run, if anything.

    ``None`` when no model is configured -- `create_workflow` refuses a
    workflow that calls `ctx.ai` in that state, so this only happens when the
    model was removed afterwards, and the step says so rather than the run
    failing somewhere less obvious.

    ``simulate`` is the dry run: the question is never asked, and the answer is
    invented from the schema the call gave. It applies only to a model the
    gates called a side effect, because a model on this machine has nothing to
    simulate -- asking it for real is what makes a dry run worth running.

    ``ask`` is injectable for the same reason ``call_tool`` is.
    """
    if simulate:
        return AiBridge(ask=_inventing, risk=risk)
    if ask is None:
        model = read_model(paths.model)
        if model is None:
            return None
        ask = _asking(model)
    return AiBridge(ask=ask, risk=risk)


async def _inventing(system: str, user: str, schema: dict[str, Any] | None) -> Answer:
    """The dry run's answer: the shape that was asked for, filled in.

    No tokens, because none were spent -- and a reader of the journal can tell a
    simulated step from a real one by exactly that.
    """
    return Answer(stand_in(schema if schema else {"type": "string"}))


def _asking(model: Model) -> Ask:
    """Bind the configured model into the shape the runner injects.

    ``${VAR}`` is expanded here, one call at a time, rather than when the file
    was read: a key exported after the daemon started should work, and a key
    that is missing should fail the step with a message naming it.
    """

    async def ask(system: str, user: str, schema: dict[str, Any] | None) -> Answer:
        return await ask_model(
            model.resolved(), system=system, user=user, schema=schema
        )

    return ask


def ai_risk(model: Model | None) -> Risk:
    """How risky it is to reach this model. Distance decides.

    A model on this machine has sent nothing anywhere, so an AI step against it
    is a read. A remote one hands the run's data to somebody else, which is a
    side effect whatever it does with it afterwards.
    """
    return READ_ONLY if model is None or model.is_local else SIDE_EFFECT


def side_effects_of(
    policy: Policy, record: dict[str, Any], model: Model | None = None
) -> list[dict[str, Any]]:
    """What in this workflow acts on the world, under today's policy.

    The risk pinned on the version is what the tool was when the workflow was
    compiled. `policy.yaml` may have been edited since, and an edit that marks a
    tool dangerous has to reach workflows that already exist -- otherwise the
    override protects nothing you already built. It only ever tightens: relaxing
    a pinned `side_effect` would need a new version, which is the safe direction
    to require paperwork in.

    An AI step is the exception to the pinning: nothing about the model is
    stored on the version, because the model is a property of the machine and
    can be changed between two runs of the same workflow. Its risk is worked out
    from whatever is configured *now*.
    """
    acting = [
        {"connector": str(t["connector"]), "tool": str(t["tool"])}
        for t in record["tools_used"]
        if _effective_risk(policy, t) != "read_only"
    ]
    if record.get("uses_ai") and _ai_risk_under(policy, model) != READ_ONLY:
        acting.append({"connector": AI_CONNECTOR, "tool": AI_TOOL})
    return acting


def _ai_risk_under(policy: Policy, model: Model | None) -> str:
    """`ai_risk`, with the machine's own opinion on this particular model.

    Overridable in both directions, unlike a tool's pinned risk: "I trust this
    provider with this data" and "even a local model is not allowed to see this"
    are both sentences a user gets to say, and the model name is the key --

    .. code-block:: yaml

        risk:
          ai:
            gpt-4o-mini: read_only
    """
    return policy.risk_for(
        AI_CONNECTOR, model.model if model else "", ai_risk(model)
    )


def _fail(
    conn: Connection,
    run_id: str,
    record: dict[str, Any],
    code: str,
    error: str,
    hint: str,
    *,
    dry_run: bool,
    **extra: Any,
) -> dict[str, Any]:
    """Close a run that will not go on, and shape the answer. One place for both.

    The two callers are the gates and the runner, and they must agree: a run
    turned away at admission and one that died reaching for a connector are the
    same thing to whoever is reading the journal afterwards.
    """
    finish_run(conn, run_id, status=STATUS_FAILED, error=error)
    conn.commit()
    return _refused(run_id, code, error, hint, record=record, dry_run=dry_run, **extra)


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
