# Harness v1 — Technical Specification (Python)

> Working name: `harness` (rename before launch). This document doubles as the future
> public `ARCHITECTURE.md`. It is written to be handed to a coding agent milestone
> by milestone — do not implement ahead of the current milestone.

## What v1 is

A local-first system that lets **any LLM agent** (Claude Code, Cursor, a chat app,
a local model) create, store and execute **deterministic, replayable workflows**
against the user's connected MCP servers.

Three deliverables:
1. A **CLI** (`harness init`, `harness serve`, `harness sync`), installable with
   `uvx harness` / `pipx`.
2. An **MCP server** exposing workflow tools (`create_workflow`, `list_workflows`,
   `get_workflow`, `run_workflow`, `get_skill`) to any MCP host.
3. A **SKILL.md** that teaches the agent how to write workflow code for this runtime.

The LLM writes a workflow **once**; afterwards the workflow runs with **no model
in the loop**: zero tokens, zero non-determinism. Robustness is enforced at
**create time** (static analysis + type checking), not discovered at run time.

## Locked decisions (do not revisit while implementing)

| # | Decision |
|---|---|
| D1 | `create_workflow` receives the **code as a parameter**. The harness writes the file itself under `~/.harness/workflows/`, content-hashes it, and stores it as an **immutable version**. Creating with an existing name yields a new version, never an overwrite. |
| D2 | **Tool access convention: `ctx.<connector>.<tool>(**kwargs)`.** Workflow code receives a single `ctx` object. At create time `ctx` is typed via a generated stub package `harness_types/` (`.pyi` files: one class per connector, one method per tool, input parameters and return types derived from the tool JSON Schemas, using `TypedDict`). At run time `ctx` is a plain object whose connector attributes are resolved through `__getattr__` into RPC calls to the harness process, which maps `connector.tool` to the real MCP call. Tool names are kept **verbatim** from `tools/list` (no renaming; MCP names are already snake_case-friendly). Workflow code never talks to MCP servers, the network or the OS directly. |
| D3 | **`create_workflow` is a compiler**, in this order: (1) lint via the `ast` module — reject forbidden imports and calls (`subprocess`, `os`, `sys`, `socket`, `http`, `urllib`, `requests`, `httpx`, `aiohttp`, `open()`, `exec`/`eval`, `__import__`, `importlib`) and **dynamic access on `ctx`** (`getattr(ctx, …)`, `ctx.__dict__`, `vars(ctx)`) so static analysis stays sound; (2) typecheck with **pyright** (strict on the workflow file) against `harness_types` — unknown tools, wrong keyword arguments, wrong types and wrong return types fail here with pyright's message and line number; (3) **static extraction** of every `ctx.X.Y(...)` call from the AST → `tools_used` is inferred, never declared; (4) pin the schema hash of each used tool. Errors are returned as actionable, agent-readable messages. |
| D4 | **No dry-run / preview in v1.** The only run-time gate is D6. |
| D5 | Tool risk classification: MCP annotations (`readOnlyHint`) when present; **unannotated tools default to `side_effect`**. Overridable per-tool in `policy.yaml`. |
| D6 | **HITL v1 = the conversation itself.** No durable interrupts, no webhooks, no dashboard, **no scheduler/cron** (v2). Gate: `run_workflow` refuses to execute a workflow whose `tools_used` contains any `side_effect` tool unless called with `confirm=True`. The refusal lists the side-effect tools so the agent can ask the human precisely. |
| D7 | Workflows declare an `inputs_schema` (JSON Schema) and receive `ctx.inputs` typed as a generated `TypedDict`; optionally an `outputs_schema` — the `run()` return type must match it (checked by pyright, validated again at run time with Pydantic). SKILL.md rule: inline constants are fine when they define the workflow itself (a fixed board ID, a URL); anything a user might vary between runs (dates, recipients, amounts, filters) must be a declared input, with a sensible default where possible. |
| D8 | Connector support v1: **public MCP servers by default** — stdio servers and remote servers with static API-key/header auth. Native OAuth flows are out of scope; document `mcp-remote` as the workaround for OAuth servers. |
| D9 | Stack: **Python ≥ 3.11**, official `mcp` Python SDK, `pydantic` v2, `pyright` (as a dev/runtime dependency, invoked as a subprocess), stdlib `ast` + `sqlite3`, `typer` for the CLI, `uv` for packaging. No agent framework, no ORM. Single package `harness/`. |
| D10 | Storage: SQLite at `~/.harness/harness.db`. Workflow code on disk, everything else in DB. |

## Directory layout (user machine)

```
~/.harness/
  harness.db               # SQLite
  config.json              # connectors (imported or added)
  policy.yaml              # risk overrides (optional)
  harness_types/           # generated .pyi stubs, regenerated on sync
    __init__.pyi
    ctx.pyi                # class Ctx with one attribute per connector
    connectors/<name>.pyi
  workflows/<name>/<version-hash>.py
```

## CLI

- `harness init` — creates `~/.harness/`, prompts to import MCP configs
  (`~/.claude.json`, project `.mcp.json`, Cursor config; flag `--from <path>`),
  connects to each server, runs `tools/list`, generates `harness_types/`,
  snapshots per-tool schema hashes. Prints a summary table (server, tools found,
  auth status). Servers it cannot auth against are listed as
  `skipped (oauth — see docs)`.
- `harness serve` — starts the harness MCP server on stdio (default) so any MCP
  host can add it. `--http <port>` optional.
- `harness sync` — re-runs discovery + stub generation; reports schema drift and
  which stored workflows it affects.

## MCP tools exposed by the harness

All tools return structured JSON. Descriptions must be written for LLM consumption.

### `get_skill`
Returns the full SKILL.md plus a compact index of available connectors and tools
(name, one-line description, risk) and the relevant `.pyi` excerpts. This is the
first call an agent should make.

### `create_workflow`
Input: `{ name, description, code, inputs_schema, outputs_schema? }`
Behavior: the D3 pipeline. On failure: `{ ok: false, stage: lint|typecheck|extract,
errors: [{ line, message, hint }] }`. On success: write file, hash, insert DB rows,
return `{ ok: true, workflow_id, version, tools_used: [{connector, tool, risk}],
warnings[] }`.

### `list_workflows` / `get_workflow`
List: name, description, latest version, created_at, last_run status.
Get: full record + code + schemas + `tools_used` + schema-drift status.

### `run_workflow`
Input: `{ workflow_id, inputs, confirm?: bool, version?: str }`
Semantics:
1. Validate `inputs` against `inputs_schema` (Pydantic) → reject with per-field errors.
2. Check schema drift: if any tool in `tools_used` no longer matches its pinned
   hash, refuse with a clear message (run `harness sync`, review, re-create).
3. Apply D6: if `tools_used` contains a `side_effect` tool and `confirm` is not
   True, refuse with the list of those tools.
4. Spawn the runner subprocess (`python -I`, empty environment, cwd set to a
   temp dir, `sys.path` limited to the runner shim + workflow file). Inject the
   `ctx` object; every `ctx.connector.tool(**kwargs)` call is sent over a pipe
   (JSON-RPC over stdin/stdout of the subprocess) to the harness, which performs
   the MCP call and returns the result (or error).
5. Journal every call (see schema). Validate the return value against
   `outputs_schema` if declared.
Returns `{ run_id, status: completed|failed, output, steps: [...] }`.

Agent-facing contract (spelled out in SKILL.md): call `run_workflow` without
`confirm`; if it refuses because of side effects, show the human which tools will
act, and only pass `confirm=True` after explicit approval in the conversation.

## SQLite schema (v1)

```sql
workflows(id, name, description, latest_version)
workflow_versions(id, workflow_id, version_hash, file_path, inputs_schema_json,
                  outputs_schema_json, tools_used_json,        -- [{connector,tool,risk,schema_hash}]
                  created_at)
runs(id, workflow_version_id, inputs_json, confirmed, status,  -- completed|failed
     output_json, started_at, finished_at)
steps(id, run_id, seq, connector, tool, risk, payload_json,
      result_json, status, duration_ms, error)
```

`runs` + `steps` are simultaneously: the audit log, the debug trace, and the
foundation for v2 resume. Never skip journaling to save time.

## Stub generation (`harness_types/`)

```python
# harness_types/connectors/pennylane.pyi
from typing import TypedDict

class ListTransactionsInput(TypedDict):
    from_: str      # JSON key "from" — reserved words get a trailing underscore, mapped back at runtime
    to: str

class Transaction(TypedDict): ...

class Pennylane:
    def list_transactions(self, *, from_: str, to: str) -> list[Transaction]:
        """List transactions in a period. (risk: read_only)"""
        ...

# harness_types/ctx.pyi
from .connectors.pennylane import Pennylane
from .connectors.gmail import Gmail

class Ctx:
    inputs: dict            # narrowed per-workflow via a generated Inputs TypedDict
    pennylane: Pennylane
    gmail: Gmail
```

Tools take **keyword-only** arguments generated from the input schema. Tools with
no output schema return `object` (SKILL.md tells the agent to narrow explicitly).
Docstrings carry the MCP description and the risk so agents reading the stubs see
both. JSON-Schema → TypedDict generation may use `datamodel-code-generator`.

## Workflow file contract

```python
from harness_types import Ctx

def run(ctx: Ctx) -> dict:
    txs = ctx.pennylane.list_transactions(from_=ctx.inputs["from"], to=ctx.inputs["to"])
    summary = summarize(txs)                  # pure Python, runs in the subprocess
    ctx.gmail.send_email(to=ctx.inputs["to_email"], subject="Monthly summary", body=summary)
    return {"count": len(txs)}
```

Synchronous by design in v1 (the runner shim handles the RPC); `async def run`
is rejected by lint with a clear message. Third-party imports are limited to a
small allowlist shipped with the runner (`json`, `datetime`, `re`, `math`,
`collections`, `itertools`, `statistics`, `decimal`, `dataclasses`, `typing`);
anything else fails lint. Extending the allowlist (e.g. `pandas`) is a v2 policy
decision, not a code change.

## SKILL.md (shipped with the package, served via `get_skill`)

Must cover, in this order:
1. What the harness is (one paragraph) and the create → run → confirm loop.
2. **The calling convention, concretely**: "You never import connectors. You
   receive `ctx`. Call tools as `ctx.<connector>.<tool>(**kwargs)` — e.g.
   `ctx.pennylane.list_transactions(from_=..., to=...)`. The harness resolves the
   call to the real MCP server at run time. Use static attribute access only;
   `getattr(ctx, name)` is rejected." Include the `.pyi` excerpts for the user's
   actual connectors (generated into the skill at `get_skill` time).
3. The workflow file contract above: `def run(ctx: Ctx)`, `ctx.inputs`, return
   value, the import allowlist.
4. The inputs rule (D7 wording) with a good and a bad example.
5. Forbidden patterns (D3 list) with the exact lint error names.
6. Three complete examples (read-only report; single side-effect; multi-step with
   inputs and outputs_schema). Every example must pass lint and pyright — there is
   a test for this.
Keep under 400 lines.

## Out of scope for v1 (explicit, so the coding agent doesn't drift)

Dry-run/preview · cron/scheduling · durable interrupts & resume · native OAuth ·
containers/VM sandboxing · multi-tenant · web dashboard · policy beyond
risk-override yaml · async workflows · import allowlist extension · other
workflow languages.

## Milestones (strictly in order; each ends with passing tests + a demo script)

**M1 — Skeleton & discovery.** CLI `init` (config import, connection, `tools/list`,
schema hashes), SQLite bootstrap, `harness_types/` generation for one stdio
server (use the official `everything` demo server in tests). Acceptance:
`harness init --from fixtures/mcp.json` produces stubs that pyright accepts + a
populated DB.

**M2 — The compiler.** MCP server scaffolding + `create_workflow` / `list` / `get`
/ `get_skill`; ast lint, pyright typecheck, static tool extraction, immutable
versioning, schema pinning. Acceptance: fixtures for each forbidden pattern fail
with a distinct error; a workflow calling an unknown tool or passing a wrong
keyword fails at typecheck with a line number; `tools_used` is inferred correctly
for a multi-connector workflow; creating twice under one name yields two versions.

**M3 — Run.** Runner subprocess (isolated interpreter, empty env), `ctx` object +
JSON-RPC bridge, runtime resolution incl. reserved-word key mapping, risk
classification (D5), the `confirm` gate (D6), journaling, drift refusal, output
validation. Acceptance: a fixture workflow with 2 reads + 1 send is refused
without `confirm`, completes with it, and both attempts are fully journaled.

**M4 — SKILL.md + policy.yaml + DX.** Skill file (with lint/pyright-clean
examples, tested), `get_skill` with live connector index, policy overrides, error
messages pass a "would an agent know what to do next?" review. Acceptance: Claude
Code on a fresh machine, with only the harness MCP server added, completes
get_skill → create → run → confirm end-to-end without human help.

**M5 — Polish for launch.** README (reuse marketing doc), `harness sync` drift
report, demo script (qwen-local friendly), small-model compat notes, `uv build`
+ PyPI publish dry run.
