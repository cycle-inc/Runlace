#!/usr/bin/env bash
#
# M6 acceptance: the authoring loop, end to end, on a live server.
#
# create -> dry run -> edit -> dry run -> refused -> confirmed. The workflow is
# written with a bug pyright cannot see, so every step has to earn its place:
#
#   1. create_workflow compiles it and says it has never run.
#   2. dry_run_workflow finds the bug on real data, and nothing acts.
#   3. edit_workflow fixes it with one string, as a new version.
#   4. The previous version is still there, byte for byte (D1).
#   5. A second dry run passes, and the side effect is stood in -- the reads
#      came back from the server, the toggle came back from its schema.
#   6. A dry run is not "when this last ran".
#   7. run_workflow still refuses without confirm (D6), and completes with it --
#      and this time the toggle really was called.
#
# Runs against a real @modelcontextprotocol/server-everything over stdio, in a
# throwaway Runlace home. Needs npx.

set -euo pipefail

cd "$(dirname "$0")/.."

RUNLACE_HOME="$(mktemp -d)/.runlace"
export RUNLACE_HOME
trap 'rm -rf "$(dirname "$RUNLACE_HOME")"' EXIT

export PYTHONPATH="$PWD/scripts"

step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

step "runlace init --from tests/fixtures/mcp_m3.json"
uv run runlace init --from tests/fixtures/mcp_m3.json

step "1. create_workflow: it compiles, and it says it has never run"
uv run python - <<'PY'
import sys
from runlace.paths import paths as runlace_paths
from runlace.server import build_server

from _m6 import CODE, INPUTS, OUTPUTS, call

result = call(
    build_server(runlace_paths()),
    "create_workflow",
    name="heat-check",
    description="Average the cities over a limit, and log it when any are.",
    code=CODE,
    inputs_schema=INPUTS,
    outputs_schema=OUTPUTS,
)
if not result["ok"]:
    for error in result["errors"]:
        print(f"  line {error['line']}: {error['message']}\n    {error['hint']}")
    sys.exit(f"FAIL: rejected at {result['stage']}")

if not any("dry_run_workflow" in w for w in result["warnings"]):
    sys.exit(f"FAIL: nothing pointed at the dry run -- {result['warnings']}")

print(f"stored heat-check {result['version']}")
print(f"  warning: {[w for w in result['warnings'] if 'dry_run' in w][0]}")
PY

step "2. dry_run_workflow finds the bug pyright could not, and nothing acts"
uv run python - <<'PY'
import sys
from runlace.paths import paths as runlace_paths
from runlace.server import build_server

from _m6 import NOBODY_OVER, call

result = call(
    build_server(runlace_paths()),
    "dry_run_workflow",
    workflow_id="heat-check",
    inputs=NOBODY_OVER,
)
if result["ok"] or result["code"] != "workflow-failed":
    sys.exit(f"FAIL: the dry run did not fail on the bug -- {result}")
if result["detail"]["type"] != "ZeroDivisionError":
    sys.exit(f"FAIL: failed for the wrong reason -- {result['detail']}")
if result["dry_run"] is not True:
    sys.exit("FAIL: the result did not say it was a dry run")

# The reads are not simulated: it got that far on what the server really said.
reads = [s for s in result["steps"] if s["tool"] == "get-structured-content"]
if len(reads) != 2 or not all(s["status"] == "ok" for s in reads):
    sys.exit(f"FAIL: the live reads did not happen -- {result['steps']}")

print(f"caught at line {result['detail']['line']}: {result['detail']['message']}")
print(f"  after {len(reads)} live reads, and {result['simulated']} held back")
PY

step "3. edit_workflow: one string, a new version"
uv run python - <<'PY'
import sys
from runlace.paths import paths as runlace_paths
from runlace.server import build_server

from _m6 import BUG, FIX, call

server = build_server(runlace_paths())
before = call(server, "get_workflow", workflow="heat-check")

result = call(
    server, "edit_workflow", name="heat-check", old_string=BUG, new_string=FIX
)
if not result["ok"]:
    sys.exit(f"FAIL: the edit was rejected -- {result['errors']}")
if result["version"] == before["version"]:
    sys.exit("FAIL: the edit did not produce a new version")

# 4. The version we edited is still on disk, unchanged.
after = call(server, "get_workflow", workflow="heat-check")
old = call(
    server, "get_workflow", workflow="heat-check", version=before["version"]
)
if old["code"] != before["code"]:
    sys.exit("FAIL: the edit rewrote the version it was editing (D1)")
if FIX not in after["code"] or len(after["versions"]) != 2:
    sys.exit(f"FAIL: the new version is not what was asked for -- {after['code']}")
# The contract carried over: it was never re-sent.
if after["outputs_schema"] != before["outputs_schema"]:
    sys.exit("FAIL: the outputs_schema did not carry over")

print(f"{before['version']} -> {result['version']}, both readable")
PY

step "5. A second dry run passes, and the side effect is stood in"
uv run python - <<'PY'
import json
import sys

from runlace.db import connect, list_steps
from runlace.paths import paths as runlace_paths
from runlace.server import build_server

from _m6 import EVERYONE_OVER, call

result = call(
    build_server(runlace_paths()),
    "dry_run_workflow",
    workflow_id="heat-check",
    inputs=EVERYONE_OVER,
)
if not result["ok"]:
    sys.exit(f"FAIL: the fixed workflow still does not dry-run -- {result}")
if result["simulated"] != [
    {"connector": "everything", "tool": "toggle-simulated-logging"}
]:
    sys.exit(f"FAIL: the wrong tools were held back -- {result['simulated']}")

conn = connect(runlace_paths().db)
steps = {s["tool"]: s for s in list_steps(conn, result["run_id"])}
if json.loads(steps["get-structured-content"]["result_json"] or "null") is None:
    sys.exit("FAIL: a live read came back empty")
# Nothing came back from a server, because nothing was sent to one: the tool
# declares no outputSchema, so there was not even a shape to stand in with.
if steps["toggle-simulated-logging"]["result_json"] is not None:
    sys.exit("FAIL: the side effect returned a server's answer")
if conn.execute("SELECT dry_run FROM runs WHERE id = ?", (result["run_id"],)).fetchone()[
    "dry_run"
] != 1:
    sys.exit("FAIL: the run was not journaled as a dry run")

print(f"output {result['output']}, {len(result['steps'])} steps, one never sent")
PY

step "6. A dry run is not \"when this last ran\""
uv run python - <<'PY'
import sys
from runlace.paths import paths as runlace_paths
from runlace.server import build_server

from _m6 import call

listed = call(build_server(runlace_paths()), "list_workflows")["workflows"][0]
if listed["last_run"] is not None:
    sys.exit(f"FAIL: a dry run was counted as a run -- {listed['last_run']}")
print(f"{listed['name']}: last_run is still None after two dry runs")
PY

step "7. run_workflow: refused without confirm (D6), completed with it"
uv run python - <<'PY'
import json
import sys

from runlace.db import connect, list_steps
from runlace.paths import paths as runlace_paths
from runlace.server import build_server

from _m6 import EVERYONE_OVER, call

server = build_server(runlace_paths())

refused = call(
    server, "run_workflow", workflow_id="heat-check", inputs=EVERYONE_OVER
)
if refused["code"] != "needs-confirmation":
    sys.exit(f"FAIL: the dry run softened the confirm gate -- {refused}")
print(f"refused: {refused['side_effects']}")

done = call(
    server,
    "run_workflow",
    workflow_id="heat-check",
    inputs=EVERYONE_OVER,
    confirm=True,
)
if not done["ok"] or done["dry_run"] is not False:
    sys.exit(f"FAIL: the confirmed run did not complete for real -- {done}")

conn = connect(runlace_paths().db)
steps = {s["tool"]: s for s in list_steps(conn, done["run_id"])}
if json.loads(steps["toggle-simulated-logging"]["result_json"] or "null") is None:
    sys.exit("FAIL: the confirmed run did not really call the side effect")

listed = call(server, "list_workflows")["workflows"][0]
if (listed["last_run"] or {}).get("status") != "completed":
    sys.exit(f"FAIL: the real run was not recorded as the last one -- {listed}")

print(f"completed {done['output']}, and this time the toggle really answered")
PY

printf '\n\033[32mM6 ACCEPTANCE: PASS\033[0m\n'
