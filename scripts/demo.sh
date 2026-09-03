#!/usr/bin/env bash
#
# The two-minute demo.
#
#   init -> get_skill -> create_workflow -> refused -> confirmed -> sync
#
# Everything after `init` goes through the MCP tools an agent sees, so what
# scrolls past is what an agent would have to work with. Nothing here needs an
# API key, a GPU or a model: the point of Runlace is that the model writes the
# workflow once and is not in the loop afterwards. To watch a local model do the
# writing, see docs/SMALL_MODELS.md.
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
note() { printf '\033[2m%s\033[0m\n' "$1"; }

step "1. runlace init -- connect to the MCP servers you already have"
note "Discovery writes a typed stub per server. pyright checks them, not us."
uv run runlace init --from tests/fixtures/mcp_m3.json

step "2. get_skill -- what the agent is handed, once"
note "A document that does not change, plus an index of your live tools and their risk."
uv run python - <<'PY'
from runlace.paths import paths as runlace_paths
from runlace.server import build_server

from _demo import call

skill = call(build_server(runlace_paths()), "get_skill")
print(f"SKILL.md: {len(skill['skill'].splitlines())} lines, the same for everyone")
for connector in skill["connectors"]:
    print(f"\n{connector['connector']} ({connector['status']}) "
          f"-- {len(connector['tools'])} tools")
    for tool in connector["tools"]:
        print(f"  {tool['risk']:<12}{tool['call']}")
print(f"\nstubs sent with it: {', '.join(sorted(skill['stubs']))}")
PY

step "3. create_workflow -- the agent writes Python, the compiler decides"
note "Four stages: lint, pyright --strict, static tool extraction, schema pinning."
uv run python - <<'PY'
import sys
from runlace.paths import paths as runlace_paths
from runlace.server import build_server

from _demo import CODE, INPUTS, OUTPUTS, call

print(CODE)
result = call(
    build_server(runlace_paths()),
    "create_workflow",
    name="heat-check",
    description="Compare two cities and alert when either is over a limit.",
    code=CODE,
    inputs_schema=INPUTS,
    outputs_schema=OUTPUTS,
)
if not result["ok"]:
    for error in result["errors"]:
        print(f"  line {error['line']}: {error['message']}\n    {error['hint']}")
    sys.exit(f"FAIL: rejected at {result['stage']}")

print(f"stored as version {result['version']} -- immutable, and pinned to:")
for tool in result["tools_used"]:
    print(f"  {tool['risk']:<12}{tool['connector']}.{tool['tool']}  {tool['schema_hash'][:12]}")
PY

step "4. run_workflow -- refused, because it can act on the world"
note "Risk is read off the server's own annotations, and pinned when the workflow is stored."
uv run python - <<'PY'
import sys
from runlace.paths import paths as runlace_paths
from runlace.server import build_server

from _demo import ARGUMENTS, call

refused = call(
    build_server(runlace_paths()), "run_workflow", workflow_id="heat-check", inputs=ARGUMENTS
)
print(f"ok: {refused['ok']}   code: {refused['code']}")
print(f"error: {refused['error']}")
for tool in refused["side_effects"]:
    print(f"  would call {tool['connector']}.{tool['tool']}")
if refused["steps"]:
    sys.exit("FAIL: a refused run reached the server anyway")
print("\nNothing ran. The reads did not happen either.")
PY

step "5. run_workflow with confirm=True -- and every step is journaled"
note "Same stored version. Confirming is a decision about this run, not an edit."
uv run python - <<'PY'
import json
import sys
from runlace.paths import paths as runlace_paths
from runlace.server import build_server

from _demo import ARGUMENTS, call

result = call(
    build_server(runlace_paths()),
    "run_workflow",
    workflow_id="heat-check",
    inputs=ARGUMENTS,
    confirm=True,
)
if not result["ok"]:
    sys.exit(f"FAIL: {result.get('error')}")
for step in result["steps"]:
    print(f"  {step['connector']}.{step['tool']:<28}{step['status']:<10}{step['duration_ms']}ms")
print(f"\noutput: {json.dumps(result['output'])}")
print(f"run {result['run_id']} -- replayable, and validated against outputs_schema")
PY

step "6. The world moves. runlace sync says what that cost you."
note "A container stops, a token expires, a vendor retires a tool. Here: the server is removed."
cp "$RUNLACE_HOME/config.json" "$RUNLACE_HOME/config.before.json"
uv run python - <<'PY'
from runlace.config import write_config
from runlace.paths import paths as runlace_paths

write_config(runlace_paths().config, [])
print("config.json now lists no connectors.")
PY
uv run runlace sync || true

step "7. Put the server back, sync again."
note "Nothing was recreated. The pinned hashes match again, so the workflow runs again."
mv "$RUNLACE_HOME/config.before.json" "$RUNLACE_HOME/config.json"
uv run runlace sync

printf '\n\033[32mThe workflow outlived the conversation that wrote it.\033[0m\n'
