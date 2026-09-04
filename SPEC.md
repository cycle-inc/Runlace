# Harness v1 — Technical Specification (Python)

> Working name: `harness`. The rename happened: the package, the CLI and the home
> directory are all `runlace` / `~/.runlace/`. This document keeps the old name in
> the v1 chapter below because that chapter is a record of what was designed and
> shipped, and rewriting it would lose the audit trail. Everything from
> **[v2 — Background execution](#v2--background-execution)** onward says `runlace`.
>
> This document doubles as the future public `ARCHITECTURE.md`. It is written to be
> handed to a coding agent milestone by milestone — do not implement ahead of the
> current milestone.
>
> **Status: v1 is shipped** (M1–M6). v2 is specified below and not yet built.

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

The LLM writes a workflow **once**; afterwards the workflow runs with ~~**no model
in the loop**: zero tokens, zero non-determinism~~ **no model in the loop unless
the workflow asked for one** — see [v3](#v3--judgement-inside-a-workflow), which
adds `ctx.ai(...)` and supersedes this sentence. Robustness is enforced at
**create time** (static analysis + type checking), not discovered at run time.

## Locked decisions (do not revisit while implementing)

| # | Decision |
|---|---|
| D1 | `create_workflow` receives the **code as a parameter**. The harness writes the file itself under `~/.harness/workflows/`, content-hashes it, and stores it as an **immutable version**. Creating with an existing name yields a new version, never an overwrite. |
| D2 | **Tool access convention: `ctx.<connector>.<tool>(**kwargs)`.** Workflow code receives a single `ctx` object. At create time `ctx` is typed via a generated stub package `harness_types/` (`.pyi` files: one class per connector, one method per tool, input parameters and return types derived from the tool JSON Schemas, using `TypedDict`). At run time `ctx` is a plain object whose connector attributes are resolved through `__getattr__` into RPC calls to the harness process, which maps `connector.tool` to the real MCP call. Tool names are kept **verbatim** from `tools/list` (no renaming; MCP names are already snake_case-friendly). Workflow code never talks to MCP servers, the network or the OS directly. |
| D3 | **`create_workflow` is a compiler**, in this order: (1) lint via the `ast` module — reject forbidden imports and calls (`subprocess`, `os`, `sys`, `socket`, `http`, `urllib`, `requests`, `httpx`, `aiohttp`, `open()`, `exec`/`eval`, `__import__`, `importlib`) and **dynamic access on `ctx`** (`getattr(ctx, …)`, `ctx.__dict__`, `vars(ctx)`) so static analysis stays sound; (2) typecheck with **pyright** (strict on the workflow file) against `harness_types` — unknown tools, wrong keyword arguments, wrong types and wrong return types fail here with pyright's message and line number; (3) **static extraction** of every `ctx.X.Y(...)` call from the AST → `tools_used` is inferred, never declared; (4) pin the schema hash of each used tool. Errors are returned as actionable, agent-readable messages. |
| D4 | ~~**No dry-run / preview in v1.** The only run-time gate is D6.~~ **Superseded in M6.** `dry_run_workflow` shipped: it executes the workflow for real but answers every `side_effect` tool call with a synthesised value instead of performing it, so an agent can see the shape of what comes back before anything acts. The reason the original decision was wrong: without it, the only way for an agent to learn a tool's real output shape was to run the side effect. The D6 gate is unchanged and still the only gate on a real run. |
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

~~Dry-run/preview~~ (shipped in M6, see D4) · cron/scheduling → **v2, M9** ·
durable interrupts & resume → **v2, M7** · native OAuth · containers/VM
sandboxing · multi-tenant · ~~web dashboard~~ → an HTTP **API** in **v2, M8**;
a dashboard remains out of scope, the developer builds their own UI · policy
beyond risk-override yaml · async workflows · import allowlist extension ·
other workflow languages.

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

**M6 — What the blind tests demanded.** Written after M5, because running real
models against the harness surfaced gaps the spec had not predicted:
`dry_run_workflow` (supersedes D4) and `edit_workflow` (a model that has to
resend a whole file to fix one line burns rounds); `add_connector` and
`serve --env-file` so a connector can be added without hand-editing JSON;
`get_skill` split into an index and a separate `get_tools` for signatures, which
took the first call from 20,130 to 8,810 tokens; `get_step`, which reads one
journaled call back out with its result trimmed to its shape. Acceptance:
`mistral-large` and `qwen3:8b`, given a neutral task and no hints, get to an
accepted workflow.

---

# v2 — Background execution

**M7 is shipped. M8–M9 are not**, and [M10](#v3--judgement-inside-a-workflow)
now comes before them. Same rule as M1–M6 otherwise: strictly in order, each
ends with passing tests and a demo.

## Who the actors are

v1 assumed two parties: a human and an agent, talking in a chat window. That
assumption is wired into the design — "HITL v1 = the conversation itself" (D6)
only makes sense if the human is reading the same transcript the agent writes to.

v2 targets a third shape, and it is the one that matters commercially:

```
end user  <->  a chatbot / agent product  <->  runlace
               (built by a developer,          (a local component
                who is our customer)            inside their backend)
```

Three consequences, and every v2 decision falls out of them:

- **We never see the end user.** They are in the developer's UI. Runlace cannot
  ask them anything, cannot show them a terminal, cannot send them a prompt.
- **The developer is not present at run time either.** They wrote the
  integration months ago and went home. Anything that requires them to type a
  command before a workflow can run is not a feature, it is a support ticket.
- **So Runlace does not own the human.** It owns *state*. When a run needs a
  human, Runlace's job is to say so in a machine-readable way and wait; the
  developer's app renders it however their product renders things — a button, a
  Slack message, an email, or nothing at all.

## The constraint that shapes the implementation

The runner subprocess **cannot outlive the process that spawned it.** It is not
independent: every `ctx.<connector>.<tool>()` call travels back over a pipe to
its parent, which performs the real MCP call. Detaching it leaves it talking
into a closed pipe.

So "background" does not mean "stop awaiting the subprocess". It means **a
long-lived process owns execution**. That process is the daemon, and it is not
the same thing as the MCP transport:

- over **stdio**, the server dies when the MCP host disconnects — a run left
  going in the background there would be killed mid-flight;
- over **`--http`**, the process is already a daemon.

## Locked decisions for v2

Named, not numbered, so they can be cited without a lookup.

| Name | Decision |
|---|---|
| **journal-is-the-queue** | No Redis, no Celery, no RQ, no Temporal. The `runs` table already records every run durably; it gains states and becomes the queue. SQLite in WAL mode, one writer. A queued run survives a restart because it was never anywhere but on disk. |
| **execution-belongs-to-the-daemon** | Only `runlace serve` executes queued runs, and exactly one drainer does so (an advisory lock row in SQLite). An MCP server on stdio enqueues and polls; it never drains. If no daemon is running, `run_workflow` executes inline as in v1 **and says so in its answer** (`executed: "inline"` vs `"queued"`) — a silent difference in behaviour depending on how the user launched the server is the worst possible failure mode. |
| **waiting-is-the-caller's-choice** | `run_workflow` gains `wait: float | None`. `None` (default, unchanged from v1) blocks until the run finishes. `0` returns as soon as the run is enqueued. A number waits that many seconds and then returns whatever state the run is in. The reason this is not simply always-async: MCP hosts have request timeouts on the order of a minute, so a three-minute workflow is impossible today — but a two-second one is *better* synchronous, and forcing every caller to poll would be a downgrade. |
| **approval-is-a-run-state** | A side-effect run that has no approval is not refused — it is **parked**. The run is created with status `awaiting_approval`, and the answer carries `run_id`, the list of side-effect tools, and the validated inputs, so the developer's app has everything it needs to render a confirmation. `approve_run(run_id)` moves it to `queued`; `reject_run(run_id, reason?)` ends it as `rejected`; parked runs expire after a configurable delay so the queue does not fill with zombies. This is the durable-interrupt feature v1 declared out of scope, and it is what makes the three-party shape work: the human is reached through the developer's product, not through us. **v1's `confirm=True` keeps its meaning** — "approval was already obtained out of band" — so the chat case still works unchanged. |
| **policy-lives-in-the-developer's-code** | Whether a side-effect run parks or proceeds is set once, at integration time, in the developer's own process (`Runlace(approval=...)`). It is deliberately **not** an MCP tool and **not** a CLI command: if lifting the gate were a tool, the agent would lift it for itself in the same turn it wrote the workflow, and the gate would be theatre. It is not a CLI command either, because nobody is at a terminal — see "Who the actors are". The developer knows their product; an internal ops bot and a public agent want opposite defaults, and only they can say which they are. Default is to park. |
| **the-endpoint-is-derived** | A workflow is callable at `POST /workflows/{name}/run` the moment it is created. No registration, no deploy step, no manifest. The request body is validated against the `inputs_schema` already stored for that version, and the OpenAPI document is generated from the same schemas. This is the product moment: the agent writes a workflow and it is *live*. |
| **localhost-is-the-boundary** | The HTTP API binds `127.0.0.1` by default. Runlace is a component inside the developer's backend; their server calls it, a browser never does. A static bearer token authenticates that backend. Binding anywhere else must be an explicit flag that prints a warning, because the moment every workflow has a run URL, a non-loopback bind means anyone on the network can trigger a real side effect. Real identity, per-workflow scopes and external audit are the enterprise layer, and they graft onto this without redefining it. |
| **a-python-api-is-the-fourth-deliverable** | v1 shipped three ways in: a CLI, an MCP server, a SKILL.md. A developer embedding Runlace in a chatbot backend has neither a terminal nor an MCP host — they have a Python process. `from runlace import Runlace` becomes a supported, documented surface, and the MCP server becomes one caller of it rather than the only door. |

## Schema delta

```sql
-- runs.status: was  completed|failed
--              now  queued|running|awaiting_approval|completed|failed|rejected|expired
ALTER TABLE runs ADD COLUMN queued_at TEXT;       -- when it entered the queue
ALTER TABLE runs ADD COLUMN approved_at TEXT;     -- NULL unless it was parked
ALTER TABLE runs ADD COLUMN approval_note TEXT;   -- reason, on reject

-- exactly one drainer; the daemon renews `heartbeat` while it holds the lock
queue_lock(id, owner_pid, hostname, heartbeat)
```

`runs` and `steps` keep their v1 job — audit log and debug trace — and the queue
is not a second store beside them, it is a column on the first one.

## What stays out of scope in v2

Retries and backoff (a failed run is a failed run; the developer decides) ·
checkpointing and mid-workflow resume (that is Temporal's problem and a
different product) · a web dashboard · multi-tenant · distributed workers ·
containers.

## Milestones

**M7 — The queue.** `runs` gains its states and the two timestamp columns; the
daemon drains with a concurrency limit and a single-drainer lock; `run_workflow`
gains `wait`; `get_run` reports state; the stdio inline fallback is explicit in
the answer. Approval parking lands here too, with `approve_run` / `reject_run`
and expiry, because a parked run is a queue state and splitting it across two
milestones would mean designing the state machine twice. No HTTP at all.
Acceptance: a workflow enqueued with `wait=0` returns a `run_id` immediately and
reaches `completed` without the caller ever blocking; a side-effect workflow
parks, is approved, and runs, with the whole sequence journaled; killing and
restarting the daemon leaves a queued run still queued and it completes.

*Shipped.* Two things the spec had not settled, decided while building it.
**Parking needs somewhere to come back to**, so it only happens when a daemon
is draining; over stdio nothing would ever resume the run, and the v1 refusal
stays the right answer there. **What the gates decided is stored on the run**
(`runs.side_effects_json`), not worked out again when a worker picks it up: a
human says yes to the list they were shown, not to whatever `policy.yaml` says
an hour later. Still open, and deferred to M8 with the HTTP layer: a daemon
killed mid-flight leaves its run `running` for ever — nothing reclaims an
orphan, because reclaiming one means deciding whether to re-run side effects
that may already have happened.

**M8 — The derived endpoint.** The same daemon serves `POST
/workflows/{name}/run` (202 + `run_id` + `Location`, or `?wait=N`), `GET
/runs/{id}`, `GET /runs/{id}/steps/{seq}`, the approve/reject routes, and a
generated OpenAPI document. Loopback bind, bearer token, warning on a
non-loopback bind. The Python API of
**a-python-api-is-the-fourth-deliverable** is what the routes call.
Acceptance: create a workflow through MCP, then run it with `curl` without
touching the CLI; a bad body is rejected against the stored `inputs_schema` with
per-field errors; a side-effect workflow returns a parked run and completes
after a `POST` to its approve route.

**M9 — Triggers.** Cron and inbound webhooks, both of which do one thing:
enqueue a run. This is where "vibe automating" becomes literal — an agent writes
an automation that runs every morning without anyone in the loop. Small, because
M7 built the queue and M8 built the entry points. Acceptance: a scheduled
workflow fires on time, is journaled with its trigger recorded, and a
side-effect one parks instead of firing blind.

---

# v3 — Judgement inside a workflow

**M10 is next, ahead of M8 and M9.** The order is now M10 → M8 → M9. Nothing in
M10 needs the HTTP layer or the scheduler, and a workflow that can only move
data between MCP servers cannot do most of what people actually want automated.

## What changes, and what does not

A workflow today is plumbing. It can call tools, branch on their output, and
shape a result — but every branch has to be expressible as Python written months
earlier by an agent that had never seen the data. "Is this invoice a duplicate",
"which of these forty tickets is the same bug", "write the reply" — none of that
is a `if` statement, and today the only way to get it is to hand the data back
to the calling agent and let it decide, which costs a round trip, leaks the raw
data into the chat transcript, and cannot happen at all in a scheduled run where
no agent is listening.

So a workflow gains **one new call**:

```python
verdict = ctx.ai(
    system="You classify expense lines. Answer only with the schema.",
    user=f"Line: {line['label']} — {line['amount']}€",
    schema={"type": "object",
            "properties": {"category": {"type": "string"},
                           "confidence": {"type": "number"}},
            "required": ["category", "confidence"]},
)
if verdict["confidence"] > 0.8:
    ctx.pennylane.categorise(line_id=line["id"], category=verdict["category"])
```

**The honest cost.** v1's headline was "zero tokens, zero non-determinism". Half
of that is now gone and it should be said plainly rather than quietly dropped
from the README:

- **Determinism dies** at every `ctx.ai` call. Same inputs, possibly different
  output. A workflow that uses it is no longer replayable in the strict sense.
- **Replayability, auditability and the gates survive**, and they are what the
  product was actually selling. The *structure* is still compiled, still
  typechecked, still pinned to real tool schemas; judgement is delegated at
  **named, typed, journaled points** rather than smeared across an agent loop.
  Every call, its prompts, its answer and its token count land in `steps`.
- **Workflows that do not call `ctx.ai` are unchanged** — zero tokens, fully
  deterministic, exactly as before. This is opt-in per workflow, per line.

The one-sentence version for the README: *the agent decides the shape once; the
workflow runs that shape forever, and asks a model only where you told it to.*

## Locked decisions for v3

Named, not numbered, as in v2.

| Name | Decision |
|---|---|
| **the-model-is-declared-once** | The model, its endpoint and its key are configured **at `runlace init`** (or `runlace model set`) and live in `~/.runlace/model.json`. Workflow code never names a model, a provider, a temperature or a token budget — `ctx.ai(system=…, user=…, schema=…)` is the whole surface. Two reasons and both are load-bearing: the agent writing the workflow is not the party who pays for inference or answers for where the data went, so it does not get to choose; and a workflow that hard-codes `gpt-4o-mini` is a workflow that breaks the day the developer switches to a local model. A per-call `model=` override is deliberately deferred — it is easy to add later and impossible to remove. |
| **one-openai-shaped-client** | Ollama, LiteLLM, OpenRouter, vLLM, llama.cpp and the commercial APIs all speak `POST /v1/chat/completions`. Runlace implements that one shape over `httpx` and nothing else. No provider SDKs, no LangChain, no litellm-as-a-dependency: a `base_url` plus a `model` plus an optional key covers every backend a user will plausibly have, and the ones it does not cover, LiteLLM already proxies. |
| **the-schema-is-the-contract** | With a `schema`, `ctx.ai` returns a `dict` **validated against that JSON Schema with Pydantic** before the workflow ever sees it. Invalid output is retried **once**, with the validation error fed back to the model as a further turn; a second failure fails the step like any other tool error. The schema is also sent to the backend as `response_format: json_schema` when it accepts one, but validation happens locally regardless, because "the provider said it supports it" is not evidence. Without a `schema`, `ctx.ai` returns the raw `str`. |
| **runtime-typed-not-static** | `ctx.ai(..., schema=...)` is typed `dict[str, Any]` in the stubs, not a generated `TypedDict`. pyright therefore will **not** catch `verdict["categorie"]`. This is a real hole and it is accepted on purpose: the schema is a literal in the workflow body, so deriving a static type from it means running the compiler over the AST of a dict literal and synthesising a `.pyi` per call site — a large amount of machinery for a guarantee the run-time validation already provides, one line later, with a better error message. Revisit only if it bites in practice. |
| **distance-decides-the-risk** | An AI step is a tool call and gets a risk like any other. **A model on loopback (`localhost`, `127.0.0.1`, `[::1]`) is `read_only`**: nothing left the machine. **Anything else is `side_effect`**, because sending the user's invoice lines to a third party *is* an effect on the world, and it is exactly the effect a developer wants a gate on. So a workflow using a remote model parks for approval unless the developer allowed it, and a dry run **simulates** the remote call while **really running** the local one. Overridable per model in `policy.yaml`, same mechanism as any other tool. |
| **an-ai-step-is-a-journaled-step** | No new table, no new trace format. An AI call is a row in `steps` with `connector = "ai"`, the prompts as its payload, the answer as its result, and two new columns for tokens. Whatever the journal already does — truncation budgets, `get_step`, timings, the failure path — it does for AI steps for free. `ai` becomes a **reserved connector name**: `add_connector` refuses it. |

## Configuration

A new file, deliberately not `config.json`: `write_config` rewrites that
document wholesale and every existing caller would erase a model section it does
not know about. An inference backend is also not an MCP connector and modelling
it as one would mean faking a `tools/list`.

```jsonc
// ~/.runlace/model.json
{
  "version": 1,
  "base_url": "http://localhost:11434/v1",   // Ollama; LiteLLM, OpenRouter, … all fit
  "model": "qwen3:8b",
  "api_key": "${OPENROUTER_API_KEY}",        // optional, ${VAR} as in config.json
  "timeout": 120
}
```

`${VAR}` expansion is the same code path as connectors — **a key is never
written to disk**. No file means no model configured, and `create_workflow`
**refuses at create time** a workflow that calls `ctx.ai` with a message telling
the agent to have the human run `runlace model set`. Failing at create time
rather than at run time is the whole point of the compiler.

## Schema delta

```sql
-- v7
ALTER TABLE steps ADD COLUMN tokens_in  INTEGER;   -- NULL for tool steps
ALTER TABLE steps ADD COLUMN tokens_out INTEGER;
```

Cost is the first question anyone asks about a workflow that calls a model, and
the journal is the only place that can answer it.

## What stays out of scope in v3

The model calling tools by itself (that is an agent, and the whole thesis is
that the *workflow* holds the control flow) · streaming · multi-turn
conversations inside one `ctx.ai` call · embeddings, vision, audio · a per-call
`model=` override · enforced cost budgets and rate limits · caching identical
prompts · fine-tuning anything.

## Milestone

**M10 — AI steps.** Four increments, each with tests, in this order.

1. **The backend.** `model.json` read/write with `${VAR}` expansion, `runlace
   model set` / `runlace model show`, the question added to `runlace init`, and
   an OpenAI-shaped client over `httpx` returning text plus token counts.
2. **The bridge.** `ctx.ai` on the shim, travelling the existing pipe as
   `connector="ai"`; `render_ctx_stub` gains an `Ai` class with two overloads
   (`str` without a schema, `dict[str, Any]` with one); `extract_tool_calls`
   learns the one-attribute-deep shape so the compiler sees the call; create-time
   refusal when no model is configured or when a `schema` is not a valid JSON
   Schema literal; `steps` v7 and the token columns.
3. **The gates.** Risk from the configured `base_url`, taking the stricter of
   pinned and current as `_effective_risk` already does; parking and `confirm`
   behave for a remote model exactly as for `gmail.send_email`; dry run
   synthesises the remote answer from the schema and really calls a local model.
4. **The story.** SKILL.md teaches `ctx.ai` — when to reach for it, why the
   model is not the agent's to choose, and that a schema is not optional in
   practice — and the "no model in the loop" claim is rewritten in README,
   SKILL.md and the MCP server's instructions.

Acceptance: a workflow that classifies its input with a local Ollama model runs
end to end and its AI step is in the journal with prompts, answer and token
counts; a schema-violating answer is retried once and then fails the step with a
readable message; the same workflow pointed at a remote `base_url` parks for
approval instead of running; a dry run of it returns a synthesised answer and
makes no HTTP request; `create_workflow` refuses `ctx.ai` when no model is
configured.
