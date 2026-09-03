#!/usr/bin/env bash
#
# M5 acceptance: the drift report, and the artifact.
#
#   1. `runlace sync` on an unchanged home reports nothing and exits 0.
#   2. A server that goes away is named, and so is the workflow it breaks --
#      exit 1, so this is usable from cron.
#   3. Put it back and sync is green again, without recreating anything.
#   4. `uv build` produces a wheel that carries SKILL.md, the typing marker and
#      the licence, and `uv publish --dry-run` accepts both artifacts.
#   5. The wheel, installed on its own with no repo around it, runs.
#
# Needs npx. Step 5 downloads Runlace's dependencies into a throwaway
# environment, so the first run is slower than the rest put together.

set -euo pipefail

cd "$(dirname "$0")/.."
REPO="$PWD"

RUNLACE_HOME="$(mktemp -d)/.runlace"
export RUNLACE_HOME
trap 'rm -rf "$(dirname "$RUNLACE_HOME")"' EXIT

export PYTHONPATH="$PWD/scripts"

step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

step "Set a home up and store a workflow in it"
uv run runlace init --from tests/fixtures/mcp_m3.json --no-verify
uv run python - <<'PY'
import sys
from runlace.paths import paths as runlace_paths
from runlace.server import build_server

from _demo import CODE, INPUTS, OUTPUTS, call

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
print(f"stored heat-check {result['version']}")
PY

step "1. A sync that finds the same servers is green"
uv run runlace sync | tee /tmp/runlace-m5-clean.txt
grep -q "all still runnable" /tmp/runlace-m5-clean.txt || {
    echo "FAIL: an unchanged sync did not report the workflow as runnable"; exit 1; }
grep -q "0 tool(s) added, 0 removed, 0 changed" /tmp/runlace-m5-clean.txt || {
    echo "FAIL: an unchanged sync invented a change"; exit 1; }

step "2. The server goes away: named tools, named workflow, exit 1"
cp "$RUNLACE_HOME/config.json" "$RUNLACE_HOME/config.before.json"
uv run python -c "
from runlace.config import write_config
from runlace.paths import paths
write_config(paths().config, [])
"
set +e
uv run runlace sync | tee /tmp/runlace-m5-drift.txt
status=${PIPESTATUS[0]}
set -e
[ "$status" -eq 1 ] || { echo "FAIL: a broken sync exited $status, not 1"; exit 1; }
grep -q -- "- everything.get-structured-content" /tmp/runlace-m5-drift.txt || {
    echo "FAIL: the tool diff did not name the removed tool"; exit 1; }
grep -q "heat-check" /tmp/runlace-m5-drift.txt || {
    echo "FAIL: the report did not name the workflow that broke"; exit 1; }
grep -q "no longer exists" /tmp/runlace-m5-drift.txt || {
    echo "FAIL: the report did not say why"; exit 1; }

step "3. Put it back: green again, and still the same stored version"
mv "$RUNLACE_HOME/config.before.json" "$RUNLACE_HOME/config.json"
uv run runlace sync | tee /tmp/runlace-m5-back.txt
grep -q "all still runnable" /tmp/runlace-m5-back.txt || {
    echo "FAIL: restoring the server did not restore the workflow"; exit 1; }
uv run python - <<'PY'
import sys
from runlace.paths import paths as runlace_paths
from runlace.server import build_server

from _demo import call

record = call(build_server(runlace_paths()), "get_workflow", workflow="heat-check")
if len(record["versions"]) != 1:
    sys.exit(f"FAIL: sync created versions -- {len(record['versions'])} now exist")
print(f"still one version: {record['version']}")
PY

step "4. uv build, then what the wheel actually carries"
rm -rf dist
uv build
uv run python - <<'PY'
import sys
import zipfile
from pathlib import Path

wheel = next(Path("dist").glob("*.whl"))
names = set(zipfile.ZipFile(wheel).namelist())
missing = [
    name
    for name in (
        "runlace/SKILL.md",
        "runlace/py.typed",
        "runlace-0.1.0.dist-info/licenses/LICENSE",
        "runlace-0.1.0.dist-info/entry_points.txt",
    )
    if name not in names
]
if missing:
    sys.exit(f"FAIL: the wheel is missing {', '.join(missing)}")
print(f"{wheel.name}: {len(names)} entries, SKILL.md and py.typed included")
PY
# No credentials here, and none wanted: --dry-run stops before the upload.
uv publish --dry-run --trusted-publishing never --token dry-run

step "5. The wheel on its own, with no repo around it"
WHEEL="$(ls "$REPO"/dist/*.whl)"
FRESH="$(mktemp -d)/.runlace"
(
    cd "$(mktemp -d)"
    RUNLACE_HOME="$FRESH" uv run --isolated --no-project --with "$WHEEL" \
        runlace init --from "$REPO/tests/fixtures/mcp_m3.json" --no-verify
    RUNLACE_HOME="$FRESH" uv run --isolated --no-project --with "$WHEEL" runlace sync
)
rm -rf "$(dirname "$FRESH")"

printf '\n\033[32mM5 ACCEPTANCE: PASS\033[0m\n'
