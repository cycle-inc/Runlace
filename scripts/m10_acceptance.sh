#!/usr/bin/env bash
#
# M10 acceptance: judgement inside a workflow, against a model that really answers.
#
# One workflow -- read a city's temperature off a live MCP server, then ask the
# model whether it is coat weather -- put through every claim the milestone makes:
#
#   1. create_workflow refuses ctx.ai while no model is configured.
#   2. `runlace model set` picks one, once, for the machine.
#   3. The same workflow now compiles, and is marked as using AI without the
#      model appearing anywhere in tools_used.
#   4. It runs end to end on a local Ollama, and the journal holds the prompts,
#      the answer and what it cost in tokens.
#   5. Pointed at a remote endpoint, the same workflow parks for confirmation.
#   6. A dry run of that answers from the schema and sends nothing.
#   7. A backend that answers with the wrong shape is asked twice, then the step
#      fails with a message that says so.
#
# Runs against a real @modelcontextprotocol/server-everything over stdio and a
# real Ollama, in a throwaway Runlace home. Needs npx and `ollama serve`.

set -euo pipefail

cd "$(dirname "$0")/.."

MODEL="${OLLAMA_MODEL:-qwen3:8b}"
OLLAMA="${OLLAMA_HOST_URL:-http://localhost:11434}"

RUNLACE_HOME="$(mktemp -d)/.runlace"
export RUNLACE_HOME
trap 'rm -rf "$(dirname "$RUNLACE_HOME")"' EXIT

export PYTHONPATH="$PWD/scripts"
export M10_MODEL="$MODEL"
export M10_BASE_URL="$OLLAMA/v1"

step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

step "0. a real model is listening"
if ! curl -sf "$OLLAMA/api/tags" | grep -q "\"$MODEL\""; then
    printf '\033[31mNo %s at %s. Start `ollama serve` and `ollama pull %s`.\033[0m\n' \
        "$MODEL" "$OLLAMA" "$MODEL"
    exit 1
fi
echo "$MODEL is up at $OLLAMA"

step "runlace init --from tests/fixtures/mcp_m3.json (no model yet)"
uv run runlace init --from tests/fixtures/mcp_m3.json --yes

step "1. create_workflow refuses ctx.ai while no model is configured"
uv run python - <<'PY'
import sys
from runlace.paths import paths as runlace_paths
from runlace.server import build_server

from _m10 import CODE, INPUTS, OUTPUTS, call

result = call(
    build_server(runlace_paths()),
    "create_workflow",
    name="coat-check",
    description="Read a city's temperature, then say whether to take a coat.",
    code=CODE,
    inputs_schema=INPUTS,
    outputs_schema=OUTPUTS,
)
if result["ok"]:
    sys.exit("FAIL: ctx.ai was accepted with no model configured")
codes = [e.get("code") for e in result["errors"]]
if "no-model-configured" not in codes:
    sys.exit(f"FAIL: refused for the wrong reason -- {result['errors']}")

error = next(e for e in result["errors"] if e.get("code") == "no-model-configured")
print(f"refused at line {error['line']}: {error['message']}")
print(f"  hint: {error['hint']}")
PY

step "2. runlace model set: one model, for the machine"
uv run runlace model set "$MODEL" --base-url "$M10_BASE_URL"

step "3. it compiles now, and the model is not a tool"
uv run python - <<'PY'
import sys
from runlace.paths import paths as runlace_paths
from runlace.server import build_server

from _m10 import CODE, INPUTS, OUTPUTS, call

server = build_server(runlace_paths())
result = call(
    server,
    "create_workflow",
    name="coat-check",
    description="Read a city's temperature, then say whether to take a coat.",
    code=CODE,
    inputs_schema=INPUTS,
    outputs_schema=OUTPUTS,
)
if not result["ok"]:
    sys.exit(f"FAIL: rejected at {result['stage']} -- {result['errors']}")

stored = call(server, "get_workflow", workflow="coat-check")
if stored["uses_ai"] is not True:
    sys.exit("FAIL: the workflow was not marked as using AI")
tools = [f"{t['connector']}.{t['tool']}" for t in stored["tools_used"]]
if any(t.startswith("ai.") for t in tools):
    sys.exit(f"FAIL: the model was pinned as if it were a connector -- {tools}")
if tools != ["everything.get-structured-content"]:
    sys.exit(f"FAIL: the wrong tools were pinned -- {tools}")

print(f"stored coat-check {result['version']}: uses_ai, tools_used {tools}")
PY

step "4. it runs end to end, and the journal says what the model was asked"
uv run python - <<'PY'
import sys
from runlace.paths import paths as runlace_paths
from runlace.server import build_server

from _m10 import CHICAGO, call

server = build_server(runlace_paths())

# No confirm: a model on this machine has sent nothing anywhere, so an AI step
# against it is a read.
done = call(server, "run_workflow", workflow_id="coat-check", inputs=CHICAGO)
if not done["ok"]:
    sys.exit(f"FAIL: the run did not complete -- {done}")

ai_steps = [s for s in done["steps"] if s["connector"] == "ai"]
if len(ai_steps) != 1:
    sys.exit(f"FAIL: expected exactly one AI step -- {done['steps']}")
ai = ai_steps[0]
if ai["risk"] != "read_only":
    sys.exit(f"FAIL: a local model was journaled as {ai['risk']}")
if not ai.get("tokens", {}).get("in") or not ai["tokens"].get("out"):
    sys.exit(f"FAIL: the AI step reported no tokens -- {ai}")

detail = call(server, "get_step", run_id=done["run_id"], seq=ai["seq"])
payload = detail["payload"]
if not payload["system"] or not payload["user"] or payload["schema"] is None:
    sys.exit(f"FAIL: the prompts were not journaled -- {payload}")
if set(detail["result"]) != {"advice", "coat"}:
    sys.exit(f"FAIL: the answer was not the shape the step asked for -- {detail['result']}")

print(f"output {done['output']}")
print(f"  asked: {payload['user']}")
print(f"  answered: {detail['result']}  ({ai['tokens']['in']} in, {ai['tokens']['out']} out)")
PY

step "5. pointed at a remote endpoint, the same workflow parks for confirmation"
uv run python - <<'PY'
import os
import sys

from runlace.model import Model, write_model
from runlace.paths import paths as runlace_paths
from runlace.server import build_server

from _m10 import CHICAGO, NOWHERE, call

paths = runlace_paths()
# The code did not change, the version did not change, the connectors did not
# change. Only where the model lives did.
write_model(paths.model, Model(base_url=NOWHERE, model=os.environ["M10_MODEL"]))

refused = call(
    build_server(paths), "run_workflow", workflow_id="coat-check", inputs=CHICAGO
)
if refused["code"] != "needs-confirmation":
    sys.exit(f"FAIL: a remote model ran without confirming -- {refused}")
if {"connector": "ai", "tool": "complete"} not in refused["side_effects"]:
    sys.exit(f"FAIL: the model call was not what it parked on -- {refused}")

print(f"refused: {refused['side_effects']}")
PY

step "6. a dry run of that answers from the schema and sends nothing"
uv run python - <<'PY'
import sys
from time import perf_counter

from runlace.paths import paths as runlace_paths
from runlace.server import build_server

from _m10 import CHICAGO, call

started = perf_counter()
result = call(
    build_server(runlace_paths()),
    "dry_run_workflow",
    workflow_id="coat-check",
    inputs=CHICAGO,
)
elapsed = perf_counter() - started

if not result["ok"]:
    sys.exit(f"FAIL: the dry run did not complete -- {result}")
if {"connector": "ai", "tool": "complete"} not in result["simulated"]:
    sys.exit(f"FAIL: the model call was not held back -- {result['simulated']}")
# The endpoint is routed nowhere: a request to it would hang until the 120s
# timeout. Coming back in seconds is the proof that none was sent.
if elapsed > 20:
    sys.exit(f"FAIL: something waited on the network -- {elapsed:.0f}s")

ai = next(s for s in result["steps"] if s["connector"] == "ai")
if "tokens" in ai:
    sys.exit(f"FAIL: an invented answer was billed -- {ai}")
if set(result["output"]) != {"city", "temperature", "advice", "coat"}:
    sys.exit(f"FAIL: the stand-in did not fill the output -- {result['output']}")

# The read is not simulated: the temperature is the real one.
print(f"invented in {elapsed:.1f}s, no tokens: {result['output']}")
PY

step "7. a wrong-shaped answer is asked twice, then fails readably"
uv run python - <<'PY'
import sys

from runlace.model import Model, write_model
from runlace.paths import paths as runlace_paths
from runlace.server import build_server

from _m10 import CHICAGO, BadBackend, call

paths = runlace_paths()

with BadBackend() as backend:
    write_model(paths.model, Model(base_url=backend.base_url, model="stubborn"))
    result = call(
        build_server(paths), "run_workflow", workflow_id="coat-check", inputs=CHICAGO
    )
    asked = backend.asked

if result["ok"]:
    sys.exit(f"FAIL: a wrong-shaped answer was accepted -- {result}")
if asked != 2:
    sys.exit(f"FAIL: the model was asked {asked} times, not twice")

ai = next(s for s in result["steps"] if s["connector"] == "ai")
if ai["status"] != "error" or "twice" not in (ai["error"] or ""):
    sys.exit(f"FAIL: the failure does not say what went wrong -- {ai}")

print(f"asked {asked} times, then: {ai['error']}")
print(f"  the run: {result['code']} -- {result['detail']['message']}")
PY

printf '\n\033[32mM10 ACCEPTANCE: PASS\033[0m\n'
