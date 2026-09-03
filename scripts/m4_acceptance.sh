#!/usr/bin/env bash
#
# M4 acceptance:
#   everything below goes through the MCP tools an agent sees -- get_skill,
#   create_workflow, run_workflow -- and nothing else. The workflow is written
#   from what get_skill returned: if the index or the stubs were not enough, the
#   compile would fail here.
#
#   Then the other half of M4: a policy.yaml edit re-classifies a tool, and the
#   workflow that already exists is gated by it without being recreated.
#
# Runs against a real @modelcontextprotocol/server-everything over stdio, in a
# throwaway Runlace home. Needs npx.

set -euo pipefail

cd "$(dirname "$0")/.."

RUNLACE_HOME="$(mktemp -d)/.runlace"
export RUNLACE_HOME
trap 'rm -rf "$(dirname "$RUNLACE_HOME")"' EXIT

# The steps below each run as their own process and share `scripts/_m4.py`.
export PYTHONPATH="$PWD/scripts"

step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

step "runlace init --from tests/fixtures/mcp_m3.json"
uv run runlace init --from tests/fixtures/mcp_m3.json

step "get_skill: is what an agent gets back enough to write a workflow?"
uv run python - <<'PY'
import sys
from runlace.paths import paths as runlace_paths
from runlace.server import build_server

from _m4 import call

skill = call(build_server(runlace_paths()), "get_skill")

document = skill["skill"]
print(f"skill document: {len(document.splitlines())} lines")
for connector in skill["connectors"]:
    print(f"\n{connector['connector']} ({connector['status']})")
    for tool in connector["tools"][:6]:
        print(f"  {tool['risk']:<12}{tool['call']:<44}{tool['description'][:40]}")
    print(f"  ... {len(connector['tools'])} tools in total")
print(f"\nstubs: {', '.join(sorted(skill['stubs']))}")

problems = []
for section in (
    "## The calling convention",
    "## The workflow file contract",
    "## The inputs rule",
    "## Forbidden patterns",
    "## Three complete examples",
):
    if section not in document:
        problems.append(f"the skill document is missing `{section}`")
if len(document.splitlines()) >= 400:
    problems.append("the skill document is over the 400-line budget")

index = {t["tool"]: t for c in skill["connectors"] for t in c["tools"]}
for tool, risk in (("echo", "read_only"), ("toggle-simulated-logging", "side_effect")):
    if tool not in index:
        problems.append(f"the index does not list `{tool}`")
    elif index[tool]["risk"] != risk:
        problems.append(f"`{tool}` is indexed as {index[tool]['risk']}, not {risk}")
if not any("connectors/everything.pyi" in name for name in skill["stubs"]):
    problems.append("the stub for the connected server was not sent")
if problems:
    sys.exit("FAIL: " + "; ".join(problems))
print("\nDocument, live index with risks, and stubs: OK")
PY

step "create_workflow: a read-only workflow, written from that index"
uv run python - <<'PY'
import sys
from runlace.paths import paths as runlace_paths
from runlace.server import build_server

from _m4 import CODE, INPUTS, call

print(CODE)
result = call(
    build_server(runlace_paths()),
    "create_workflow",
    name="tally",
    description="Echo a message and add two numbers.",
    code=CODE,
    inputs_schema=INPUTS,
)
if not result["ok"]:
    for error in result["errors"]:
        print(f"    line {error['line']}: {error['message']}\n      {error['hint']}")
    sys.exit(f"FAIL: rejected at {result['stage']}")

print(f"version: {result['version']}")
for tool in result["tools_used"]:
    print(f"  {tool['risk']:<12}{tool['connector']}.{tool['tool']}")

risks = {t["tool"]: t["risk"] for t in result["tools_used"]}
if risks != {"echo": "read_only", "get-sum": "read_only"}:
    sys.exit(f"FAIL: tools_used is {risks}")
print("\nCompiled, and pinned as read-only: OK")
PY

step "run_workflow: read-only, so it runs with no confirmation"
uv run python - <<'PY'
import json
import sys
from runlace.paths import paths as runlace_paths
from runlace.server import build_server

from _m4 import ARGUMENTS, call

result = call(
    build_server(runlace_paths()), "run_workflow", workflow_id="tally", inputs=ARGUMENTS
)
print(json.dumps(result, indent=2)[:800])

if not result["ok"] or "42" not in result["output"]["total"]:
    sys.exit(f"FAIL: {result.get('error') or result['output']}")
print("\nRan unconfirmed, because nothing it calls acts on the world: OK")
PY

step "policy.yaml: the operator disagrees -- get-sum is a side effect here"
uv run python - <<'PY'
import textwrap
from runlace.paths import paths as runlace_paths

policy = runlace_paths().policy
policy.write_text(
    textwrap.dedent(
        """\
        risk:
          everything:
            get-sum: side_effect
        """
    ),
    encoding="utf-8",
)
print(policy.read_text())
PY

step "The workflow that already exists is now gated -- no recreate, no new version"
uv run python - <<'PY'
import json
import sys
from runlace.paths import paths as runlace_paths
from runlace.server import build_server

from _m4 import ARGUMENTS, call

server = build_server(runlace_paths())
refused = call(server, "run_workflow", workflow_id="tally", inputs=ARGUMENTS)
print(json.dumps(refused, indent=2)[:600])

problems = []
if refused["code"] != "needs-confirmation":
    problems.append(f"refused with `{refused.get('code')}`")
if [t["tool"] for t in refused.get("side_effects", [])] != ["get-sum"]:
    problems.append(f"the refusal names {refused.get('side_effects')}")
if refused["steps"]:
    problems.append("a refused run performed tool calls")

confirmed = call(
    server, "run_workflow", workflow_id="tally", inputs=ARGUMENTS, confirm=True
)
if not confirmed["ok"]:
    problems.append(f"the confirmed run failed: {confirmed.get('error')}")
if confirmed["version"] != refused["version"]:
    problems.append("the override forced a new version")

if problems:
    sys.exit("FAIL: " + "; ".join(problems))
print(f"\nRefused, then ran on confirm -- still version {confirmed['version']}: OK")
PY

step "A typo in policy.yaml is reported, not silently ignored"
uv run python - <<'PY'
import pathlib
import sys
import textwrap
from runlace.init_cmd import run_init
from runlace.paths import paths as runlace_paths

paths = runlace_paths()
paths.policy.write_text(
    textwrap.dedent(
        """\
        risk:
          everything:
            get-sum: side_effect
            get_sunm: read_only
        """
    ),
    encoding="utf-8",
)

report = run_init(paths, [pathlib.Path("tests/fixtures/mcp_m3.json")])
for warning in report.warnings:
    print(f"warning: {warning}")

if not any("get_sunm" in w for w in report.warnings):
    sys.exit("FAIL: the typo was accepted in silence")
print("\nThe override that gates nothing says so: OK")
PY

printf '\n\033[32mM4 ACCEPTANCE: PASS\033[0m\n'
