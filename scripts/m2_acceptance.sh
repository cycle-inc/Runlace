#!/usr/bin/env bash
#
# M2 acceptance:
#   - fixtures for each forbidden pattern fail with a distinct error
#   - an unknown tool, and a wrong keyword argument, fail at typecheck with a
#     line number
#   - tools_used is inferred correctly for a multi-connector workflow
#   - creating twice under one name yields two versions
#
# Runs against two real @modelcontextprotocol/server-everything instances over
# stdio, in a throwaway Runlace home. Needs npx.

set -euo pipefail

cd "$(dirname "$0")/.."

RUNLACE_HOME="$(mktemp -d)/.runlace"
export RUNLACE_HOME
trap 'rm -rf "$(dirname "$RUNLACE_HOME")"' EXIT

step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

step "runlace init --from tests/fixtures/mcp_m2.json (two connectors)"
uv run runlace init --from tests/fixtures/mcp_m2.json

step "Forbidden patterns: each must fail with its own error"
uv run python - <<'PY'
import sys
from runlace.lint import lint

TEMPLATE = """\
from runlace_types import Ctx
%s

def run(ctx: Ctx) -> dict[str, object]:
    %s
    return {}
"""

FIXTURES = [
    ("import subprocess",   "import subprocess", "pass"),
    ("import os",           "import os", "pass"),
    ("import sys",          "import sys", "pass"),
    ("import socket",       "import socket", "pass"),
    ("import http.client",  "import http.client", "pass"),
    ("from urllib import…", "from urllib.request import urlopen", "pass"),
    ("import requests",     "import requests", "pass"),
    ("import httpx",        "import httpx", "pass"),
    ("import aiohttp",      "import aiohttp", "pass"),
    ("import importlib",    "import importlib", "pass"),
    ("import pandas",       "import pandas", "pass"),
    ("open()",              "", 'handle = open("/etc/passwd")'),
    ("exec()",              "", 'exec("1")'),
    ("eval()",              "", 'value = eval("1")'),
    ("__import__()",        "", '__import__("os")'),
    ("getattr(ctx, …)",     "", 'tool = getattr(ctx, "everything")'),
    ("vars(ctx)",           "", "table = vars(ctx)"),
    ("ctx.__dict__",        "", "table = ctx.__dict__"),
    ("ctx passed along",    "", "helper = print; helper(ctx)"),
    ("ctx reassigned",      "", "ctx = None"),
    ("connector aliased",   "", "server = ctx.everything"),
    ("tool not called",     "", "send = ctx.everything.echo"),
]

ASYNC_RUN = """\
from runlace_types import Ctx


async def run(ctx: Ctx) -> dict[str, object]:
    return {}
"""
NO_RUN = "from runlace_types import Ctx\n\n\ndef helper() -> int:\n    return 1\n"

cases = [(label, TEMPLATE % (preamble, body)) for label, preamble, body in FIXTURES]
cases.append(("async def run", ASYNC_RUN))
cases.append(("no run()", NO_RUN))

print(f"{'FIXTURE':<22}{'LINE':<6}{'ERROR CODE':<28}MESSAGE")
print("-" * 108)
messages, problems = [], []
for label, code in cases:
    errors = lint(code)
    if not errors:
        problems.append(f"{label}: accepted")
        continue
    error = errors[0]
    print(f"{label:<22}{error.line:<6}{error.code:<28}{error.message}")
    messages.append(error.message)
    if not error.hint:
        problems.append(f"{label}: no hint")

if len(set(messages)) != len(messages):
    problems.append("two fixtures produced the same error message")
if problems:
    sys.exit("FAIL: " + "; ".join(problems))
print(f"\n{len(cases)} forbidden patterns, {len(set(messages))} distinct errors: OK")
PY

step "Unknown tool and wrong keyword: rejected at typecheck, with a line number"
uv run python - <<'PY'
import sys
from runlace.db import connect
from runlace.paths import paths as runlace_paths
from runlace.workflows import create_workflow

paths = runlace_paths()
conn = connect(paths.db)
INPUTS = {"type": "object", "properties": {"message": {"type": "string"}},
          "required": ["message"]}

CASES = {
    "unknown tool": (
        "from runlace_types import Ctx\n\n\n"
        "def run(ctx: Ctx) -> dict[str, object]:\n"
        '    value = ctx.everything.no_such_tool(message="hi")\n'
        '    return {"value": value}\n'
    ),
    "unknown connector": (
        "from runlace_types import Ctx\n\n\n"
        "def run(ctx: Ctx) -> dict[str, object]:\n"
        '    value = ctx.slack.post_message(text="hi")\n'
        '    return {"value": value}\n'
    ),
    "wrong keyword": (
        "from runlace_types import Ctx\n\n\n"
        "def run(ctx: Ctx) -> dict[str, object]:\n"
        '    value = ctx.everything.echo(text="hi")\n'
        '    return {"value": value}\n'
    ),
    "wrong argument type": (
        "from runlace_types import Ctx\n\n\n"
        "def run(ctx: Ctx) -> dict[str, object]:\n"
        "    value = ctx.everything.get_sum(a=1, b=\"two\")\n"
        '    return {"value": value}\n'
    ),
    "undeclared input": (
        "from runlace_types import Ctx\n\n\n"
        "def run(ctx: Ctx) -> dict[str, object]:\n"
        '    return {"x": ctx.inputs["never_declared"]}\n'
    ),
}

problems = []
for label, code in CASES.items():
    result = create_workflow(conn, paths, name="rejected", description="probe",
                             code=code, inputs_schema=INPUTS)
    print(f"\n--- {label}: ok={result.ok} stage={result.stage}")
    for error in result.errors:
        print(f"    line {error.line}: {error.message}")
        print(f"       hint: {error.hint}")
    if result.ok:
        problems.append(f"{label}: accepted")
        continue
    if result.stage != "typecheck":
        problems.append(f"{label}: failed at {result.stage}, expected typecheck")
    if any(e.line != 5 for e in result.errors):
        problems.append(f"{label}: wrong or missing line number")

stored = conn.execute("SELECT COUNT(*) FROM workflows").fetchone()[0]
if stored:
    problems.append(f"{stored} rejected workflow(s) were stored anyway")
if problems:
    sys.exit("FAIL: " + "; ".join(problems))
print("\nAll rejected at typecheck, on the right line, and none were stored: OK")
PY

step "A multi-connector workflow: tools_used is inferred, not declared"
uv run python - <<'PY'
import json
import sys
from runlace.db import connect
from runlace.paths import paths as runlace_paths
from runlace.workflows import create_workflow, get_workflow, list_workflows

paths = runlace_paths()
conn = connect(paths.db)

INPUTS = {
    "type": "object",
    "properties": {"message": {"type": "string"}, "topic": {"type": "string"}},
    "required": ["message", "topic"],
}
OUTPUTS = {
    "type": "object",
    "properties": {"greeting": {"type": "string"}},
    "required": ["greeting"],
}

V1 = '''\
from runlace_types import Ctx, Output


def run(ctx: Ctx) -> Output:
    greeting = ctx.everything.echo(message=ctx.inputs["message"])
    ctx.demo.simulate_research_query(topic=ctx.inputs["topic"])
    return {"greeting": str(greeting)}
'''

V2 = '''\
from runlace_types import Ctx, Output


def run(ctx: Ctx) -> Output:
    greeting = ctx.everything.echo(message=ctx.inputs["message"])
    total = ctx.everything.get_sum(a=1, b=2)
    ctx.demo.simulate_research_query(topic=ctx.inputs["topic"])
    return {"greeting": f"{greeting} ({total})"}
'''

print(V1)
first = create_workflow(conn, paths, name="research-digest",
                        description="Echo a message and kick off a research query.",
                        code=V1, inputs_schema=INPUTS, outputs_schema=OUTPUTS)
if not first.ok:
    for error in first.errors:
        print(f"    line {error.line}: {error.message}")
    sys.exit(f"FAIL: version 1 was rejected at {first.stage}")

print(f"workflow_id: {first.workflow_id}")
print(f"version:     {first.version}\n")
print(f"{'CONNECTOR':<14}{'TOOL (verbatim)':<28}{'RISK':<13}PINNED SCHEMA HASH")
print("-" * 74)
for tool in first.tools_used:
    print(f"{tool['connector']:<14}{tool['tool']:<28}{tool['risk']:<13}"
          f"{tool['schema_hash'][:16]}")
for warning in first.warnings:
    print(f"\nwarning: {warning}")

# The tool name is the verbatim MCP one (D2): the workflow spells it
# `simulate_research_query`, the server calls it `simulate-research-query`.
inferred = {(t["connector"], t["tool"]) for t in first.tools_used}
expected = {("everything", "echo"), ("demo", "simulate-research-query")}
problems = []
if inferred != expected:
    problems.append(f"tools_used is {sorted(inferred)}, expected {sorted(expected)}")
if not any(t["risk"] == "side_effect" for t in first.tools_used):
    problems.append("the side-effecting tool was not classified as one")
if any(len(t["schema_hash"]) != 64 for t in first.tools_used):
    problems.append("a tool is missing its pinned schema hash")

print("\n--- creating again under the same name")
second = create_workflow(conn, paths, name="research-digest",
                         description="Echo a message and kick off a research query.",
                         code=V2, inputs_schema=INPUTS, outputs_schema=OUTPUTS)
if not second.ok:
    for error in second.errors:
        print(f"    line {error.line}: {error.message}")
    sys.exit(f"FAIL: version 2 was rejected at {second.stage}")
print(f"version:     {second.version}")
print(f"tools_used:  {sorted((t['connector'], t['tool']) for t in second.tools_used)}")

record = get_workflow(conn, "research-digest")
print("\n--- get_workflow(\"research-digest\")")
print(f"versions:    {[v['version'] for v in record['versions']]}")
print(f"latest:      {record['version']}")
print(f"drift:       {record['drift']}")
print(f"stored at:   {record['file_path']}")

print("\n--- list_workflows()")
print(json.dumps(list_workflows(conn), indent=2))

if first.workflow_id != second.workflow_id:
    problems.append("the second create made a new workflow instead of a version")
if first.version == second.version:
    problems.append("two different bodies produced the same version")
if len(record["versions"]) != 2:
    problems.append(f"expected 2 versions, found {len(record['versions'])}")
if record["version"] != second.version:
    problems.append("the latest version is not the one created last")
if not record["drift"]["ok"]:
    problems.append("a freshly created workflow already reports schema drift")

files = sorted(p.name for p in (paths.workflows / "research-digest").iterdir())
print(f"\nfiles on disk: {files}")
if files != sorted([f"{first.version}.py", f"{second.version}.py"]):
    problems.append("the earlier version's file is missing or was overwritten")

if problems:
    sys.exit("FAIL: " + "; ".join(problems))
print("\nMulti-connector inference, pinning and versioning: OK")
PY

step "Resubmitting identical code does not create a third version"
uv run python - <<'PY'
import sys
from runlace.db import connect
from runlace.paths import paths as runlace_paths
from runlace.workflows import create_workflow, get_workflow

paths = runlace_paths()
conn = connect(paths.db)

INPUTS = {
    "type": "object",
    "properties": {"message": {"type": "string"}, "topic": {"type": "string"}},
    "required": ["message", "topic"],
}
OUTPUTS = {
    "type": "object",
    "properties": {"greeting": {"type": "string"}},
    "required": ["greeting"],
}

V2 = '''\
from runlace_types import Ctx, Output


def run(ctx: Ctx) -> Output:
    greeting = ctx.everything.echo(message=ctx.inputs["message"])
    total = ctx.everything.get_sum(a=1, b=2)
    ctx.demo.simulate_research_query(topic=ctx.inputs["topic"])
    return {"greeting": f"{greeting} ({total})"}
'''

again = create_workflow(conn, paths, name="research-digest",
                        description="Echo a message and kick off a research query.",
                        code=V2, inputs_schema=INPUTS, outputs_schema=OUTPUTS)
print(f"ok={again.ok} created={again.created} version={again.version}")
for warning in again.warnings:
    print(f"warning: {warning}")

record = get_workflow(conn, "research-digest")
count = len(record["versions"])
print(f"versions in the database: {count}")
if again.created or count != 2:
    sys.exit(f"FAIL: expected the existing version back, found {count} versions")
print("OK")
PY

printf '\n\033[32mM2 ACCEPTANCE: PASS\033[0m\n'
