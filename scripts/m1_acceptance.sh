#!/usr/bin/env bash
#
# M1 acceptance:
#   runlace init --from fixtures/mcp.json
#     -> stubs that pyright accepts
#     -> a populated DB
#
# Runs against the real @modelcontextprotocol/server-everything over stdio,
# in a throwaway Runlace home. Needs npx.

set -euo pipefail

cd "$(dirname "$0")/.."

RUNLACE_HOME="$(mktemp -d)/.runlace"
export RUNLACE_HOME
trap 'rm -rf "$(dirname "$RUNLACE_HOME")"' EXIT

step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
fail() { printf '\033[31mFAIL: %s\033[0m\n' "$1" >&2; exit 1; }

step "runlace init --from tests/fixtures/mcp.json"
uv run runlace init --from tests/fixtures/mcp.json

step "Generated layout"
find "$RUNLACE_HOME" -type f | sed "s|$RUNLACE_HOME|~/.runlace|" | sort

step "Stubs pyright checks against (excerpt)"
sed -n '1,12p' "$RUNLACE_HOME/runlace_types/connectors/everything.pyi"
printf '...\n'
grep -m2 '    def ' "$RUNLACE_HOME/runlace_types/connectors/everything.pyi"

step "ctx.pyi"
cat "$RUNLACE_HOME/runlace_types/ctx.pyi"

step "Populated database"
uv run python - <<'PY'
import os, sqlite3, sys
db = os.path.join(os.environ["RUNLACE_HOME"], "runlace.db")
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row

tables = sorted(r["name"] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table'"))
print("tables:", ", ".join(tables))

connectors = conn.execute("SELECT * FROM connectors").fetchall()
tools = conn.execute("SELECT * FROM tools ORDER BY name").fetchall()

print()
print(f"{'TOOL (verbatim)':<34}{'METHOD':<34}{'RISK':<13}SCHEMA HASH")
print("-" * 92)
for t in tools:
    print(f"{t['name']:<34}{t['method']:<34}{t['risk']:<13}{t['schema_hash'][:16]}")

print()
for c in connectors:
    print(f"connector {c['name']}: status={c['status']} tools={c['tool_count']}")

problems = []
if not connectors:
    problems.append("no connectors recorded")
if not tools:
    problems.append("no tools recorded")
if any(c["status"] != "connected" for c in connectors):
    problems.append("a connector failed to connect")
if any(len(t["schema_hash"]) != 64 for t in tools):
    problems.append("a tool is missing its schema hash")
if any(t["risk"] not in ("read_only", "side_effect") for t in tools):
    problems.append("a tool is missing its risk classification")
if problems:
    sys.exit("DB check failed: " + "; ".join(problems))
print("\nDB check: OK")
PY

step "A workflow written against the stubs typechecks"
cat > "$RUNLACE_HOME/probe.py" <<'PY'
from runlace_types import Ctx


def run(ctx: Ctx) -> dict[str, object]:
    echoed = ctx.everything.echo(message="hello")
    total = ctx.everything.get_sum(a=1, b=2)
    return {"echoed": echoed, "total": total}
PY
cat "$RUNLACE_HOME/probe.py"
uv run python - <<'PY'
import os, pathlib, sys
from runlace.typecheck import check_paths
home = pathlib.Path(os.environ["RUNLACE_HOME"])
r = check_paths(home, [home / "probe.py"], types_dir=home / "runlace_types")
print(r.report())
if not r.ok:
    sys.exit("probe workflow failed to typecheck")
PY

step "Negative control: an unknown tool must be rejected"
cat > "$RUNLACE_HOME/bad.py" <<'PY'
from runlace_types import Ctx


def run(ctx: Ctx) -> dict[str, object]:
    return {"x": ctx.everything.no_such_tool()}
PY
uv run python - <<'PY'
import os, pathlib, sys
from runlace.typecheck import check_paths
home = pathlib.Path(os.environ["RUNLACE_HOME"])
r = check_paths(home, [home / "bad.py"], types_dir=home / "runlace_types")
print(r.report())
if r.ok:
    sys.exit("an unknown tool typechecked -- the stubs are not being enforced")
print("correctly rejected")
PY

printf '\n\033[32mM1 ACCEPTANCE: PASS\033[0m\n'
