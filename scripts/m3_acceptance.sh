#!/usr/bin/env bash
#
# M3 acceptance:
#   a fixture workflow with 2 reads + 1 send is refused without `confirm`,
#   completes with it, and both attempts are fully journaled.
#
# Runs against a real @modelcontextprotocol/server-everything over stdio, in a
# throwaway Runlace home. Needs npx.

set -euo pipefail

cd "$(dirname "$0")/.."

RUNLACE_HOME="$(mktemp -d)/.runlace"
export RUNLACE_HOME
trap 'rm -rf "$(dirname "$RUNLACE_HOME")"' EXIT

step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

step "runlace init --from tests/fixtures/mcp_m3.json"
uv run runlace init --from tests/fixtures/mcp_m3.json

step "The fixture workflow: two reads and one send"
uv run python - <<'PY'
import sys
from runlace.db import connect
from runlace.paths import paths as runlace_paths
from runlace.workflows import create_workflow

CODE = '''\
from runlace_types import Ctx, Output


def run(ctx: Ctx) -> Output:
    greeting = ctx.everything.echo(message=ctx.inputs["message"])
    total = ctx.everything.get_sum(a=ctx.inputs["a"], b=ctx.inputs["b"])
    ctx.everything.toggle_simulated_logging()
    return {"greeting": str(greeting), "total": str(total)}
'''

INPUTS = {
    "type": "object",
    "properties": {
        "message": {"type": "string"},
        "a": {"type": "number"},
        "b": {"type": "number"},
    },
    "required": ["message", "a", "b"],
}
OUTPUTS = {
    "type": "object",
    "properties": {"greeting": {"type": "string"}, "total": {"type": "string"}},
    "required": ["greeting", "total"],
}

paths = runlace_paths()
conn = connect(paths.db)

print(CODE)
result = create_workflow(
    conn, paths, name="daily-digest",
    description="Echo a message, add two numbers, and flip the server's logging.",
    code=CODE, inputs_schema=INPUTS, outputs_schema=OUTPUTS,
)
if not result.ok:
    for error in result.errors:
        print(f"    line {error.line}: {error.message}")
    sys.exit(f"FAIL: the fixture was rejected at {result.stage}")

print(f"workflow_id: {result.workflow_id}")
print(f"version:     {result.version}\n")
print(f"{'CONNECTOR':<14}{'TOOL (verbatim)':<28}RISK")
print("-" * 55)
for tool in result.tools_used:
    print(f"{tool['connector']:<14}{tool['tool']:<28}{tool['risk']}")
for warning in result.warnings:
    print(f"\nwarning: {warning}")

risks = {t["tool"]: t["risk"] for t in result.tools_used}
expected = {"echo": "read_only", "get-sum": "read_only",
            "toggle-simulated-logging": "side_effect"}
if risks != expected:
    sys.exit(f"FAIL: tools_used is {risks}, expected {expected}")
print("\n2 reads + 1 send, inferred from the source: OK")
PY

step "Attempt 1: run_workflow WITHOUT confirm -- must be refused (D6)"
uv run python - <<'PY'
import asyncio
import json
import sys
from runlace.db import connect
from runlace.paths import paths as runlace_paths
from runlace.runs import run_workflow

ARGUMENTS = {"message": "good morning", "a": 20, "b": 22}

paths = runlace_paths()
conn = connect(paths.db)

result = asyncio.run(
    run_workflow(conn, paths, workflow="daily-digest", inputs=ARGUMENTS)
)
print(json.dumps(result, indent=2))

problems = []
if result["ok"] is not False:
    problems.append("the run was not refused")
if result["code"] != "needs-confirmation":
    problems.append(f"refused with `{result['code']}`, expected needs-confirmation")
if [t["tool"] for t in result["side_effects"]] != ["toggle-simulated-logging"]:
    problems.append(f"the refusal names {result['side_effects']}")
if result["steps"]:
    problems.append("a refused run performed tool calls")
if not result["run_id"]:
    problems.append("a refused run has no run_id to audit")
if problems:
    sys.exit("FAIL: " + "; ".join(problems))
print("\nRefused before any tool ran, and it says exactly which tool acts: OK")
PY

step "Attempt 2: run_workflow WITH confirm=True -- must complete"
uv run python - <<'PY'
import asyncio
import json
import sys
from runlace.db import connect
from runlace.paths import paths as runlace_paths
from runlace.runs import run_workflow

ARGUMENTS = {"message": "good morning", "a": 20, "b": 22}

paths = runlace_paths()
conn = connect(paths.db)

result = asyncio.run(
    run_workflow(conn, paths, workflow="daily-digest", inputs=ARGUMENTS, confirm=True)
)
print(json.dumps(result, indent=2))

problems = []
if not result["ok"]:
    problems.append(f"the run failed: {result.get('error')}")
if result["status"] != "completed":
    problems.append(f"status is {result['status']}")
called = [s["tool"] for s in result["steps"]]
if called != ["echo", "get-sum", "toggle-simulated-logging"]:
    problems.append(f"steps are {called}")
if any(s["status"] != "ok" for s in result["steps"]):
    problems.append("a step failed")
if problems:
    sys.exit("FAIL: " + "; ".join(problems))
print("\nRan all three tools against the live server and returned its output: OK")
PY

step "The journal: both attempts, in full"
uv run python - <<'PY'
import json
import sys
from runlace.db import connect, list_steps
from runlace.paths import paths as runlace_paths

paths = runlace_paths()
conn = connect(paths.db)

rows = list(conn.execute("SELECT * FROM runs ORDER BY rowid"))
print(f"{'RUN':<20}{'CONFIRMED':<11}{'STATUS':<12}{'STEPS':<7}ERROR")
print("-" * 110)
for row in rows:
    print(f"{row['id']:<20}{bool(row['confirmed'])!s:<11}{row['status']:<12}"
          f"{len(list_steps(conn, row['id'])):<7}{row['error'] or ''}")

if len(rows) != 2:
    sys.exit(f"FAIL: expected both attempts to be journaled, found {len(rows)} run(s)")
refused, confirmed = rows

print(f"\n--- refused attempt {refused['id']}")
print(f"status:      {refused['status']}")
print(f"confirmed:   {bool(refused['confirmed'])}")
print(f"inputs:      {refused['inputs_json']}")
print(f"error:       {refused['error']}")
print(f"started_at:  {refused['started_at']}")
print(f"finished_at: {refused['finished_at']}")
print(f"steps:       {len(list_steps(conn, refused['id']))}")

print(f"\n--- confirmed attempt {confirmed['id']}")
print(f"status:      {confirmed['status']}")
print(f"confirmed:   {bool(confirmed['confirmed'])}")
print(f"inputs:      {confirmed['inputs_json']}")
print(f"output:      {confirmed['output_json']}")
print(f"started_at:  {confirmed['started_at']}")
print(f"finished_at: {confirmed['finished_at']}")

steps = list_steps(conn, confirmed["id"])
print(f"\n{'SEQ':<5}{'CONNECTOR':<13}{'TOOL':<28}{'RISK':<13}{'STATUS':<8}{'MS':<6}"
      f"PAYLOAD -> RESULT")
print("-" * 130)
for step in steps:
    result = (step["result_json"] or "")[:44]
    print(f"{step['seq']:<5}{step['connector']:<13}{step['tool']:<28}{step['risk']:<13}"
          f"{step['status']:<8}{step['duration_ms']:<6}{step['payload_json']} -> {result}")

problems = []
if refused["status"] != "failed" or refused["confirmed"] != 0:
    problems.append("the refused attempt is not journaled as an unconfirmed failure")
if "toggle-simulated-logging" not in (refused["error"] or ""):
    problems.append("the refused attempt does not record why")
if refused["finished_at"] is None:
    problems.append("the refused attempt was never closed")
if list_steps(conn, refused["id"]):
    problems.append("the refused attempt journaled steps it never took")
if confirmed["status"] != "completed" or confirmed["confirmed"] != 1:
    problems.append("the confirmed attempt is not journaled as a confirmed success")
if confirmed["error"] is not None:
    problems.append("a completed run recorded an error")
if json.loads(confirmed["inputs_json"]) != json.loads(refused["inputs_json"]):
    problems.append("the two attempts do not record the same inputs")
if [s["tool"] for s in steps] != ["echo", "get-sum", "toggle-simulated-logging"]:
    problems.append("the confirmed attempt's steps are not the three tool calls")
if [s["seq"] for s in steps] != [1, 2, 3]:
    problems.append("the steps are not numbered in call order")
if any(s["payload_json"] is None for s in steps):
    problems.append("a step did not record what it sent")
if any(s["duration_ms"] is None for s in steps):
    problems.append("a step did not record how long it took")
if json.loads(steps[1]["payload_json"]) != {"a": 20, "b": 22}:
    problems.append("a step did not record the arguments it was called with")

if problems:
    sys.exit("FAIL: " + "; ".join(problems))
print("\nBoth attempts fully journaled, with every argument and every result: OK")
PY

printf '\n\033[32mM3 ACCEPTANCE: PASS\033[0m\n'
