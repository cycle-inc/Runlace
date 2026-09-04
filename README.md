# Runlace

Replayable workflows over your MCP servers. An LLM writes a workflow once;
afterwards it runs with no model in the loop, unless the workflow itself asked
for one -- see [M10](#m10--judgement-inside-a-workflow).

See `SPEC.md` for the full design. It is committed verbatim and still uses the
working name `harness` throughout; everything in this repo has since been
renamed to Runlace, including the on-disk names D2 and D10 spell out.

**This repo implements M1 through M5 — every milestone in the spec — plus M6,
the authoring loop: `dry_run_workflow` and `edit_workflow`. Ten MCP tools in
all.**

```
uv tool install runlace          # or: uvx runlace init
runlace init                     # imports the MCP configs you already have
runlace serve                    # add this to Claude Code, Cursor, anything
```

To watch the whole loop in two minutes, against a real MCP server and with no
API key: `./scripts/demo.sh`.

## M1 — skeleton & discovery

`runlace init` imports the MCP server definitions you already have, connects to
each server, calls `tools/list`, and turns the result into typed Python stubs
that pyright can check workflow code against.

```
runlace init --from tests/fixtures/mcp.json
```

produces:

```
~/.runlace/
  runlace.db               connectors + tools (+ the workflow tables, unpopulated)
  config.json              the normalised connector list
  runlace_types/
    __init__.pyi
    ctx.pyi                class Ctx, one attribute per connector
    connectors/<name>.pyi  one class per connector, one method per tool
  workflows/
```

Remote servers authenticate with static headers (D8). Write the secret as a
reference and it stays a reference:

```json
{"github": {"type": "http", "url": "https://api.githubcopilot.com/mcp/",
            "headers": {"Authorization": "Bearer ${GITHUB_PAT}"}}}
```

`${VAR}` is resolved from the environment when a connection is opened, so
`config.json` holds the reference and never the token. A reference with nothing
behind it is reported as `skipped (environment variable(s) not set: …)` rather
than sent unsubstituted and answered with a puzzling 401. `command` is the one
field left alone — it is a binary looked up on PATH.

Tool names are kept verbatim from `tools/list`; the stubs carry the Python
spelling alongside (`get-annotated-message` → `get_annotated_message`).
Reserved words in parameters get a trailing underscore (`from` → `from_`) and
the docstring records the mapping.

## M2 — the compiler

`runlace serve` starts an MCP server that any host can add. It exposes five
tools:

| tool | what it does |
| --- | --- |
| `get_skill` | how to write a workflow, plus every connector, tool and stub |
| `create_workflow` | compile a workflow and, if it passes, store a version |
| `list_workflows` | one line per workflow |
| `get_workflow` | the code, the schemas, the pinned tools, the version list |
| `run_workflow` | execute one, no model in the loop unless it calls `ctx.ai` (M3) |

```
runlace serve             # stdio
runlace serve --http 8931 # streamable HTTP
```

A workflow is one Python file with a single `run(ctx)`:

```python
from runlace_types import Ctx, Output


def run(ctx: Ctx) -> Output:
    greeting = ctx.everything.echo(message=ctx.inputs["message"])
    ctx.demo.simulate_research_query(topic=ctx.inputs["topic"])
    return {"greeting": str(greeting)}
```

`create_workflow` compiles it in four stages, and stops at the first that
fails:

1. **lint** — an `ast` pass for the patterns D3 forbids: imports outside the
   allowlist, `open`/`exec`/`eval`/`__import__`, dynamic attribute access,
   `async def`, a missing or misshapen `run`. Each has its own error code.
2. **typecheck** — pyright in strict mode against the M1 stubs, on a throwaway
   copy where `Inputs` and `Output` are narrowed to TypedDicts generated from
   the schemas you declared. An unknown tool, a misspelled keyword, a wrong
   argument type or an undeclared input key all fail here with a line number.
3. **extract** — every `ctx.<connector>.<tool>(...)` call site, read straight
   out of the AST. `tools_used` is inferred, never declared.
4. **pin** — each extracted tool is resolved against the database and its
   `schema_hash` recorded on the version, so drift is detectable later.

Versions are immutable and content-addressed (D1): creating twice under one
name adds a second version and moves the `latest_version` pointer, leaving the
first file on disk untouched. Resubmitting byte-identical content returns the
existing version instead of duplicating it.

```
~/.runlace/workflows/<name>/<version>.py
```

## M3 — the runner

`run_workflow` executes a stored version. Nothing calls a model unless the
workflow's own code does (M10); otherwise it just runs.

```
run_workflow(workflow_id, inputs, confirm=False, version=None)
  -> {run_id, status, output, steps}
```

Five things happen, in this order, and the first one to object stops the run:

1. **inputs** — validated against `inputs_schema` with Pydantic (D7), reported
   per field. JSON keys are reported verbatim, reserved words included: a bad
   `from` comes back as `from`.
2. **drift** — if any tool the version was pinned to no longer hashes to the
   same schema, the run is refused rather than attempted.
3. **confirm** (D6) — if the workflow uses any `side_effect` tool and `confirm`
   is not `True`, it is refused with the exact list of tools that would act.
   Nothing runs, not even the reads.
4. **the run** — a subprocess, described below.
5. **output** — validated against `outputs_schema` if the workflow declares
   one. The value is still returned when it fails: the side effects already
   happened, and hiding what came back would help nobody.

The workflow runs in a throwaway directory under its own interpreter
(`python -I`, empty environment) holding nothing but the runner shim, the
workflow file and its inputs. Before the workflow is executed the shim imports
D3's allowlist and then empties `sys.path` and `sys.meta_path`, so `runlace`,
the MCP clients and the rest of site-packages are unreachable — `import os`
raises `ModuleNotFoundError`. It is not a sandbox (D4 puts real isolation out of
scope); it is lint's allowlist enforced a second time, where it is cheap.

`ctx` is injected there. Every `ctx.<connector>.<tool>(**kwargs)` becomes one
line of JSON-RPC over a private pipe; Runlace resolves it to the verbatim MCP
tool name, translates `from_` back to `from`, performs the real call, translates
the result the other way, and writes a `steps` row before answering. The child's
stdout is pointed at stderr and its stdin at `/dev/null` first, so a stray
`print` cannot corrupt the protocol.

That translation is schema-driven and goes all the way down, because the stubs
rename at every level — `create_relations(relations=[{"from_": ...}])` has the
reserved word inside a list item, not in the signature. `runlace.keys` walks the
value alongside the schema and renames exactly what the stub generator renamed,
in both directions: where the generator gives up and emits `dict[str, object]`,
nothing is renamed, because nothing was promised.

Every attempt is journaled, refusals included — `runs` and `steps` are the
audit log, the debug trace and the foundation for v2 resume, so a refused run
still gets a `run_id` you can show a human.

### Not in M3

`runlace sync` and the full SKILL.md are M4. Resume, scheduling and streaming
progress are v2.

### Known gap: a compiled workflow can still guess a shape wrong

"If it compiles, it runs" is true for servers that declare an `outputSchema`,
and only partly true for those that do not — GitHub declares none on any of its
47 tools. Those calls are typed `Any`, so pyright cannot check what the workflow
does with the result, and no amount of type-system work can: nothing was
promised. Handing a model the tasks in `blind_test` produced exactly one runtime
failure of this kind, a `for` loop over a value that was a dict rather than the
expected list. It passed all four compiler stages.

Two fixes, attacking different halves. The first shipped in M4:

1. **Let the agent look before it writes.** SKILL.md tells it that when a tool's
   response shape is unclear it may call that tool itself to inspect the real
   answer — restricted to tools classified `read_only`, so looking cannot act.
   This needs no new machinery and addresses the cause: the model was guessing
   when it could have checked.
2. **A dry run.** Execute the workflow once before storing it and refuse to
   store one that crashes, turning "compiles" into "has actually run". Stronger,
   but structurally partial: D6 means a workflow that touches a side-effecting
   tool cannot be rehearsed, so the guarantee would be two-tier. It also needs
   sample inputs and makes creation slow and network-dependent.

The second is not in `SPEC.md`, and D3 enumerates the compiler as four stages.
Adding a fifth would be a change to a locked decision, so it shipped in M6 as a
separate `dry_run_workflow` tool outside the compiler instead — and without the
two-tier guarantee, because a side-effecting tool is stood in rather than
skipped.

## M4 — the skill and the policy

`get_skill` now returns the real `SKILL.md` (`src/runlace/SKILL.md`, shipped
with the package) instead of a primer: the calling convention, the file
contract, the lint codes and three worked examples, alongside this machine's
connector index and its generated stubs.

Every workflow in that document is compiled by `tests/test_skill_examples.py`,
and the lint codes its table advertises are checked against the codes that
actually exist. A skill file that teaches something the compiler rejects is
worse than none at all — the agent follows it, gets an error, and cannot tell
which of the two is wrong — so the suite goes red before that can ship.

### `policy.yaml`

D5 treats an unannotated tool as a side effect. That is the right default and it
is also unusable on a server that annotates nothing: the confirm gate fires on
every run, which is the same as it never firing. `~/.runlace/policy.yaml` is the
release valve.

```yaml
risk:
  github:
    search_repositories: read_only
    create_issue: side_effect
```

Nothing in that file can take down `init` or a run. Unreadable, malformed, or
holding a value that is not a risk — each becomes a warning and the rest is
still applied, because "your override was ignored" is easier to recover from
than "nothing works". An override that matches no tool on this machine is
reported at `init`, since that typo fails in the dangerous direction: you
believe a tool is gated and it is not.

### Not in M4

`runlace sync` as a command is M5; the dry run described above is unbuilt.
Resume, scheduling and streaming progress are v2.

## M5 — launch

### `runlace sync`

An MCP server is somebody else's software. Parameters get renamed, tools get
retired, whole servers stop answering, and D1's pinning means none of that can
silently change what a stored workflow does — it just stops running. `sync`
re-discovers every configured connector and reports the difference.

```
runlace sync
```

The tool diff is the easy half:

```
0 tool(s) added, 2 removed, 1 changed their schema
  - pennylane.get_balance
  ~ github.create_issue
```

The half that decides whether anyone has work to do is the second one:

```
1 of 4 workflow(s) would now be refused:
  weekly-report (a1a204ba0580)
      pennylane.get_balance no longer exists
```

It exits 1 when something is broken, so it belongs in cron. It regenerates the
stubs on the way through — the next workflow an agent writes is checked against
what the servers do now, not what they did at `init`. A connector that failed to
answer is called out separately, because "every tool of one server vanished"
almost always means the server is down, and recreating workflows on that
evidence would be the wrong move.

Nothing is ever rewritten. The pinned versions stay on disk and stay readable
with `get_workflow`; recreating one is a decision, and decisions are the agent's.

### Small models

Runlace's whole premise is that the model writes the workflow once, so the
question is whether a cheap local model can do the writing. `scripts/demo_agent.py`
runs that loop against any OpenAI-compatible endpoint, Ollama by default:

```
ollama serve & ollama pull qwen3:8b
./scripts/demo_agent.py --task "Echo a greeting and add two numbers"
```

What we measured, and where the floor is, is in `docs/SMALL_MODELS.md`.

### Packaging

```
uv build
uv publish --dry-run --trusted-publishing never --token dry-run
```

The wheel carries `SKILL.md` and `py.typed`; `scripts/m5_acceptance.sh` installs
it into an empty environment with no repo around it and runs `init` and `sync`
from there, because "it works in the checkout" is not the claim being made.

## M6 — the authoring loop

`SPEC.md` stops at M5. M6 is the loop the spec's five tools leave to the agent's
patience: write the whole file, run it for real, and hope. Two tools close it,
and neither touches a locked decision.

### `dry_run_workflow`

Runs the workflow for real and lets nothing act. Every read hits the live
server and returns the real answer; every tool classified `side_effect` is
answered from its own declared `outputSchema` instead of being called. Nothing
is sent, so there is nothing to confirm and there is no gate — every other gate
still applies, in the same order.

The result is a normal run result plus `{dry_run: true, simulated: [...]}`.
`simulated` is the honest part: a branch that depends on what one of those calls
really returns is the one thing a dry run cannot check, so it is named rather
than glossed over. The run is journaled like any other and marked, so it is
auditable but never counts as "when this workflow last ran".

This is what turns "it compiles" into "it has actually run". `create_workflow`
now says so on every new version, in a warning that names the tool — the same
reasoning as the confirm gate: an optional step nobody is told about is a step
nobody takes.

### `edit_workflow`

One exact string, replaced once, recompiled, stored as a new version. The
schemas and the description carry over. It must match exactly and it must match
once; Runlace will not guess which of two occurrences was meant, because
guessing wrong changes the wrong line silently.

D1 is untouched: this is `create_workflow` with less typing, not an update. The
version that was edited stays on disk, readable and runnable.

`scripts/m6_acceptance.sh` walks the whole loop against a live
`server-everything`: a workflow with a division by zero pyright cannot see, a
dry run that finds it on real data without toggling anything, a one-string fix,
a second dry run that passes, then refused-without-confirm and completed-with-it.

## M10 — judgement inside a workflow

Some steps are not code. "Is this invoice hosting or travel", "summarise this
thread in one line": no `if` gets there, and a workflow that cannot ask stops at
the first one. `ctx.ai(...)` asks.

```python
verdict = ctx.ai(
    system="You classify expenses. Answer with the category only.",
    user=f"Vendor: {tx['vendor']}. Memo: {tx['memo']}",
    schema={"type": "object", "properties": {"category": {"type": "string"}},
            "required": ["category"]},
)
```

The model is a property of the machine, not of the workflow. Whoever runs
Runlace picks it once -- `runlace init --model qwen3:8b`, or `runlace model set`
later -- and the workflow never names one. Any OpenAI-shaped endpoint works,
which is all of them: Ollama, LiteLLM, OpenRouter, vLLM, llama.cpp, the
commercial APIs.

```
runlace model set qwen3:8b                        # a local Ollama, the default
runlace model set gpt-4o-mini \
  --base-url https://api.openai.com/v1 \
  --api-key '${OPENAI_API_KEY}'                   # expanded at run time, never stored
runlace model show
```

`model set` asks the model one question before saving, so a backend that is down
or a model that was never pulled is a problem you have at configuration time
rather than three minutes into a run.

With a `schema` the answer is validated locally against it -- the same Pydantic
path as every other schema here -- and comes back as a dict; a model that
answers the wrong shape is shown the error and asked once more before the step
fails. Without a schema you get the raw string. The call is journaled like a
tool call, prompts and tokens included, so `get_step` shows what the model
actually said.

**Distance decides the risk.** A model on this machine has sent nothing
anywhere, so an AI step against it is a read. A remote one has handed the run's
data to somebody else, so it goes through the confirm gate like sending an
email, and a dry run invents its answer from the schema rather than asking.
`policy.yaml` overrides it per model name, in both directions:

```yaml
risk:
  ai:
    gpt-4o-mini: read_only
```

**The honest cost.** A workflow with an AI step is no longer deterministic: two
runs can differ. Everything else holds -- the same code, the same pinned tools,
the same journal, the same gates -- and a workflow that does not call `ctx.ai`
is exactly what it was before. It is opt-in one line at a time.

`scripts/m10_acceptance.sh` runs all of that against a real Ollama: one workflow
that reads a live temperature and then asks the model whether it is coat
weather, refused before a model is configured, journaled with its tokens after,
parked for confirmation when the model moves off the machine, and failed
readably by a backend that answers the wrong shape twice.

## Adding MCP servers after the first run

`init` imports a list of configs and writes exactly that list. That is right the
first time and wrong every time after, so three commands merge instead.

```
runlace add files --command npx \
  --arg -y --arg @modelcontextprotocol/server-filesystem --arg ~/sandbox

runlace add github --url https://api.githubcopilot.com/mcp/ \
  --header 'Authorization: Bearer ${GITHUB_TOKEN}'

runlace remove files
```

**A credential written literally is refused.** `config.json` is a file on disk;
`${VAR}` is resolved when the connection opens, so the file keeps the reference
and never the secret. Runlace tells you which variable to use:

```
$ runlace add github --url ... --header "Authorization: Bearer ghp_realtoken"
github: header `Authorization` looks like a credential, and config.json is a
file on disk. Use "${RUNLACE_GITHUB_AUTHORIZATION}" instead and export it
before the next run.
```

That variable is read by whichever process opens the connection, which is
`runlace serve` — not the shell where you ran `runlace add`. `--env-file` is
there so the two do not drift apart:

```
runlace serve --http 8000 --env-file ~/.runlace/tokens.env
```

It reports the names it loaded and never the values, and anything already in the
environment wins.

### `runlace import --from-open-webui`

If your users connect their MCP servers in the chat UI, this copies them across:

```
export OPEN_WEBUI_TOKEN=sk-...        # an Open WebUI admin API key
runlace import --from-open-webui http://localhost:3000
```

```
warning: github: uses bearer auth, and the token stays in Open WebUI --
         ${RUNLACE_GITHUB_AUTHORIZATION} stands in for it
warning: https://weather.example/openapi: type `openapi`, not an MCP server -- skipped
skip     runlace (that's me)
~        github (replaced)

SERVER      TRANSPORT  TOOLS  STATUS
----------  ---------  -----  ---------
everything  stdio      13     connected
files       stdio      14     connected
github      http       47     connected
```

The bridge only runs this way, and that is not a limitation of the code. Open
WebUI's `ToolServerConnection` has a `url` and no `command`: it can only reach
MCP servers over HTTP, and cannot launch `npx`. Runlace speaks stdio *and* HTTP,
so everything the UI knows about, Runlace can drive — never the reverse.

Four things it does on purpose:

- **Runlace skips itself.** It is registered in the UI too, and importing it
  would be a loop.
- **OpenAPI tool servers are refused.** Runlace drives MCP; writing a connector
  that can never connect is worse than saying so.
- **Credentials do not come across.** The UI keeps the real token; Runlace gets
  a `${VAR}` beside it. Re-importing will not clobber a reference you already
  set and exported — anything you typed wins over anything we generated.
- **A variable nobody exported is named before discovery runs**, because "did
  not answer" is a much worse explanation than "export `GITHUB_TOKEN`".

Once imported, disable the server in Open WebUI. It stays in the config, so the
bridge can still read it, but the model can no longer call it directly — it has
to go through Runlace, with the confirm gate and the journal.

### `add_connector`, from the chat

The CLI covers the developer. An MCP tool covers their user: "connect my Notion"
in the chat, no terminal. Same core as `runlace add` — same merge, same refusal
to write a secret down — behind the same shape of gate as `run_workflow`:

```
add_connector(name="notion", url="https://mcp.notion.com/mcp")
  -> {code: "needs-confirmation", action: "add", connector: {...}, needs_env: []}
add_connector(..., confirm=True)
  -> {ok: true, attr: "notion", tools: 19, next: "Call get_skill again ..."}
```

Two deliberate narrowings compared to the CLI:

- **No `command`.** A URL only reaches outwards; a command is "run this program
  on my machine", and the value would be arriving from a model that may have
  read it off a web page a moment earlier. Local servers are added from a shell.
- **A literal token is refused here too**, and the error tells the agent to ask
  for an `export` rather than for the token itself. It has no reason to pass
  through the conversation.

`runlace serve` reads the environment once, at startup, so a newly exported
variable needs a restart. The tool says so when it hands back `needs_env`.

## What crosses into the model's context

Both of the following were found the same way: by connecting a real MCP server
and reading what actually went over the wire. A test server with four tools
hides these completely.

### `get_skill` is an index; `get_tools` has the signatures

`get_skill` used to return every connector's whole `.pyi`. One GitHub connector
is 47 tools and 8,000 tokens of types, read in full to call three of them.
`SPEC.md` line 71 asked for "the *relevant* `.pyi` excerpts" — returning all of
them was the drift.

So the index and the signatures are two calls. `get_skill` grows one line per
tool: name, one line of description, risk. `get_tools(connector, tools)` renders
the slice of the stub covering the tools that were picked — exact signature,
which arguments are required, what comes back. It is the same text pyright will
check the workflow against, so a call written from it compiles.

The generated `<Tool>Input` TypedDicts went at the same time. Arguments are
keyword-only and the signature spells every one of them out, so the aggregate
was unreachable — workflow code cannot even import it. Types *nested* inside a
parameter stay: the signature names those. Measured on three connectors and 74
tools:

| Call | Before | After |
|---|---|---|
| `get_skill` | 20,130 tokens | **8,810** |
| `get_tools("github", 3 tools)` | — | **554** |
| `get_tools("github", all 47)` | 9,548 | 6,675 |

The `.pyi` files on disk are untouched by any of this. pyright reads those, and
it does not have a context window.

### `get_step`, and the rule that makes it rare

`run_workflow` has always reported its steps without their payloads. The journal
showed the leak had moved: workflows were returning the raw tool results as
their `output`. One four-call workflow came back with 21,387 characters — the
four payloads verbatim, under four keys — where a dozen fields were wanted. The
sandbox had been paid for and not used.

That is a `SKILL.md` problem, not a runtime one: nothing in the document said
that reducing is the job. It says so now, with the anti-pattern spelled out and
the note that it compiles — because it does, and nothing in the pipeline will
catch it.

`get_step(run_id, seq)` is the escape hatch that makes the rule liveable. It
returns one journaled call, arguments and result, trimmed: long lists cut to
their first two items, long strings to 300 characters, and a `trimmed` list
saying what was dropped and from where. Nothing is replaced by an ellipsis
inside the data — a `"... 47 more"` string sitting in a list of objects would
misreport the very shape the agent is reading it for.

Two items rather than three, because the first shows the shape and the second
shows which of its fields were optional after all. On the largest step in a real
journal that difference was 2,781 tokens against 1,981, for the same
information.

There is deliberately no flag to ask for the whole payload. The tool exists so
an agent can see what a tool *looks like* and write correct code against it;
reading a thousand rows is the workflow's job, in the subprocess. `result_chars`
reports how much it would have been.

## A chat UI to drive it with

`docker/chat/` brings up [Open WebUI](https://github.com/open-webui/open-webui)
on `http://localhost:3000`, pointed at Mistral's OpenAI-compatible API, with
Runlace registered as an MCP tool server.

```
runlace serve --http 8000 --host 0.0.0.0   # terminal 1
./scripts/chat_ui.sh up                    # terminal 2
```

Then, in the UI: **Settings → Tools → Add**, type `MCP`, URL
`http://host.docker.internal:8000/mcp`. Set the model's **Function Calling** to
`Native` in its Advanced Params — the prompt-based fallback cannot chain seven
tools.

Three things about that layout are deliberate:

- **Runlace stays on the host.** It launches your MCP servers as local
  subprocesses (`npx`, `uvx`, whatever `runlace init` found) and reads
  `~/.runlace`. Containerising it would mean rebuilding your whole local stack
  inside an image.
- **`--host 0.0.0.0`, because a container cannot reach its host's loopback.**
  `runlace serve` binds `127.0.0.1` by default and says so loudly when you widen
  it: anything that can reach that port can run a stored workflow, and
  `confirm=True` is one JSON field away. Do not do this on a shared network.
- **No `mcpo` proxy.** Open WebUI speaks MCP streamable HTTP natively since
  0.6.31, which is the transport `runlace serve --http` already speaks. A proxy
  in between would rewrite the tool descriptions, and the descriptions *are* the
  interface.

The Mistral key is read out of your `.env` at the moment compose runs and passed
through the environment; `chat_ui.sh` never writes it to a file. Point
`MISTRAL_ENV_FILE` somewhere else if yours lives elsewhere.

## Development

```
uv sync
uv run pytest                    # full suite
uv run pytest -m 'not needs_npx' # skip the tests that launch a real MCP server
uv run pyright                   # Runlace's own source and tests
./scripts/m1_acceptance.sh       # the M1 acceptance criterion, end to end
./scripts/m2_acceptance.sh       # the M2 acceptance criteria, end to end
./scripts/m3_acceptance.sh       # the M3 acceptance criterion, end to end
./scripts/m4_acceptance.sh       # the M4 acceptance criterion, end to end
./scripts/m5_acceptance.sh       # sync, the wheel, and the wheel on its own
./scripts/m6_acceptance.sh       # create -> dry run -> edit -> refuse -> confirm
./scripts/m10_acceptance.sh      # ctx.ai against a live Ollama, needs `ollama serve`
./scripts/demo.sh                # the two-minute demo
./scripts/demo_agent.py          # let a local model write the workflow
./scripts/chat_ui.sh up          # a chat UI on localhost:3000, see above
```

Tests set `RUNLACE_HOME` to a temporary directory, so they never touch your
real `~/.runlace`.

### Decisions this implementation had to make

Two things M1 needs that `SPEC.md` does not pin down:

- **`connectors` and `tools` tables.** The spec's SQL block defines only the
  four workflow tables, but D10 puts everything except workflow code in SQLite
  and M1 has to persist discovery. These two tables are additive; the four
  documented ones are unchanged.
- **`${VAR}` in connector headers, args, env and url.** D8 allows remote
  servers with static header auth, which would otherwise put a bearer token in
  plaintext in `config.json`. The spec does not say where the secret should
  live, so it lives in the environment and the config keeps a reference,
  resolved at connection time.
- **A tool with no `outputSchema` returns `Any`, not `object`.** D2 derives
  return types from the schemas, and there is nothing to derive from a schema
  that does not exist. This is not a rare gap: of the four servers tried so far,
  three declare a schema on every tool (24/24) and GitHub declares one on none
  (0/47). `object` reads as the stricter choice but is not — it cannot be
  indexed, so the author must write `cast(dict[str, object], ...)`, which
  pyright accepts on their word alone. That buys no safety over `Any` and costs
  a ritual on every call, plus a false note of reassurance to the next reader.
- **Schema-hash scope.** The per-tool hash covers `inputSchema` and
  `outputSchema` only. A server rewording a tool description will not
  invalidate stored workflows; changing a parameter will.

And these in M2:

- **An `Output` type.** D7 wants the return type checked against
  `outputs_schema` by pyright, but a generated TypedDict is assignable neither
  to nor from `dict[str, object]`, so a naive check would reject every
  workflow. `Output` is therefore a real name in `runlace_types`, permissive in
  the persistent stubs and narrowed on the compile-time copy — exactly the
  mechanism the spec already mandates for `Inputs`. Lint requires
  `-> Output` whenever `outputs_schema` is declared, so the check cannot be
  silently skipped.
- **The dynamic-access rule is a category, not three spellings.** D3 names
  `getattr(ctx, ...)`, `ctx.__dict__` and `vars(ctx)`. Matching only those is
  unsound — `c = ctx; getattr(c, "gmail")` walks past them — so the builtins
  that perform dynamic attribute access are refused outright and `ctx` may only
  ever appear as `ctx.<attribute>`, with `ctx.<connector>.<tool>` required to
  be called. That is what makes static extraction exact rather than
  best-effort.
- **What the version hash covers.** Code plus both schemas. A file whose bytes
  are unchanged but whose declared outputs differ is a different contract, and
  D1's immutability would otherwise let the two share a version.
- **Resubmitting identical content** returns the existing version with a
  warning instead of creating a duplicate row.
- **Workflow names** match `^[a-z0-9][a-z0-9_-]{0,62}$`, because the name
  becomes a directory.
- **Errors carry a `code`** alongside the `{line, message, hint}` the spec
  asks for, so a caller can branch on the failure without parsing prose.

And these in M3:

- **Refusals are journaled as failed runs.** The acceptance criterion asks for
  both attempts to be journaled, so a `runs` row is written before the first
  gate and closed as `failed` if a gate objects. That needed somewhere to say
  *why*, hence an additive `runs.error` column and a migration ladder keyed on
  `meta.schema_version` — a Runlace home is the user's data, so it is upgraded
  in place, never recreated. A run is `running` while in flight.
  The one case with no row is a workflow or version that does not exist: there
  is nothing to attach a run to.
- **The journal records both sides in the server's spelling.** A step is the
  record of what went over the wire, so `payload` and `result` hold `from`, not
  the `from_` the workflow wrote and read. The two stay consistent with each
  other and with what the server saw.
- **The `steps` in the response omit `payload` and `result`.** A step that read
  a thousand rows would drown the agent's context. Both are in the `steps`
  table, which is where a debug trace belongs.
- **Two timeouts**, neither in the spec: 300s for a whole run, 120s for a single
  tool call. Without them a workflow that loops forever, or a server that never
  answers, would hang the agent that called `run_workflow`.
- **Declared `default`s are filled in** for top-level input keys the caller left
  out, and the filled-in values are what gets journaled. Only the top level:
  inventing values inside nested objects would be guesswork.
- **`runlace_types` exists at run time as a synthetic module.** The generated
  package is `.pyi` stubs with no code behind it, so the import every workflow
  starts with would fail on its own; the shim registers a module exposing `Ctx`,
  `Inputs` and `Output` instead of putting a package on `sys.path`. Only the top
  level can work that way, so lint now rejects `runlace_types.<anything>` at
  create time rather than letting it fail at run time.
- **What a tool call returns to the workflow**: `structuredContent` when the
  tool declares an output schema — that is what the stub promised — otherwise
  the text blocks, with a lone one parsed if it holds a JSON object or array.
  Servers that declare no schema still answer in JSON; they just have nowhere
  to put it but a text block. Only objects and arrays, and only when there is
  exactly one block: `"42"` stays the string it was, and GitHub's
  `get_file_contents` answers with a sentence *and* the file, which parsing the
  first block would have thrown away. An `isError` result raises inside the
  workflow, which may catch it; the step is journaled as an error either way.
- **Sessions are opened up front**, one per connector the workflow uses, before
  any workflow code runs. A server that is down fails the run before the first
  side effect rather than halfway through.

And these in M4:

- **`pyyaml` is a dependency D9 does not list.** D9 enumerates the stack and
  names no YAML parser, but D5 puts the risk overrides in `policy.yaml` and
  `SPEC.md` keeps "policy beyond risk-override yaml" out of scope — so the yaml
  itself is in. Writing a parser rather than adding the one everybody already
  has would be the worse reading of a locked decision.
- **A policy edit only ever tightens an existing workflow.** The risk pinned on
  a version is what the tool was when it compiled; `policy.yaml` may have been
  edited since. The confirm gate takes the stricter of the two, so marking a
  tool dangerous reaches workflows that already exist — otherwise the override
  protects nothing you have already built. It cannot go the other way: relaxing
  a pinned `side_effect` needs a new version, which is the safe direction to
  require paperwork in.
- **Both spellings of a tool name are accepted** in `policy.yaml`. The server
  says `get-annotated-message` and the stub the user is reading says
  `get_annotated_message`; making them discover which one this file wanted
  would be a trap with a silent failure at the end of it.
- **A workflow may not define its own `Output`.** Found while reviewing error
  messages for M4: a `class Output(TypedDict)` in the workflow file is not a
  redefinition of anything — pyright type-checks the return value against the
  invented shape, happily, and `outputs_schema` goes unenforced until Pydantic
  rejects the result *after* the side effects have happened. Lint now requires
  `Output` to come from `runlace_types` when `outputs_schema` is declared.
- **Read-only probing is guidance in SKILL.md, not a tool.** The agent is told
  it may call a tool itself to learn the shape of its response, and only one
  classified `read_only`. Runlace does not proxy that call: a `call_tool` on
  this server would be a way around the gate it exists to enforce, and the agent
  already has its own client.
- **One typecheck hint says the schema is fine.** A list of objects assembled in
  a variable is inferred as `list[dict[...]]`, which is not a list of the
  generated item type, because lists are invariant. The value and the
  `outputs_schema` agree; only where the list was built is wrong. The generic
  "fix one or the other" would send an agent rewriting a schema that was never
  the problem, so that case gets its own hint — build the list inside the
  `return`, where the declared type gives each item its expected shape.

And these in M5:

- **`sync` writes; it is not a dry report.** It persists what it discovered and
  regenerates the stubs, exactly as `init` does, then reports the difference
  against what was there before. The alternative — report now, apply later —
  would leave the stubs describing servers that have moved on, so the next
  workflow an agent writes would be checked against yesterday. The stored
  workflow versions are the thing that never changes, and they do not.
- **`sync` exits 1 when a workflow is broken**, and 0 otherwise, so it can be a
  cron job rather than something someone remembers to read.
- **Only the latest version of each workflow is checked for drift.** Older
  versions are history; D1 keeps them readable whatever the servers do, and
  reporting that a superseded version no longer runs is noise.
- **An unreachable connector is reported apart from the tool diff.** Its tools
  do read as removed — that is what the database now says — but every tool of
  one server disappearing at once is far more often an outage than a retirement.
- **MIT, and `SPEC.md` does not say.** The spec asks for a PyPI publish dry run
  and names no licence, and PyPI needs one. MIT is the convention for a tool
  like this; the copyright line says "Runlace contributors" rather than guessing
  a legal entity. Both are one edit away if that is wrong.
- **`py.typed` ships.** The package is fully annotated and the classifiers claim
  it, so importers should get the annotations rather than `Any`.
- **Lint now checks the `Ctx` annotation and the imports behind it.** Watching a
  local 8B model work through the loop turned up a cascade: one missing
  `from runlace_types import Ctx` came back as nine pyright errors, every one of
  them a variant of "type of X is unknown" and none of them naming the line to
  add. That is one mistake, so it is now one lint error that says what to write.
  The same goes for `-> Output` without the import, which lint previously only
  looked at when an `outputs_schema` was declared.
- **Lint's hints know your connectors.** `ctx.echo(...)` is what a model writes
  after reading an index that lists tools, and the honest generic hint — "write
  `ctx.echo.<tool>(...)`" — is the same mistake one level deeper, stated with
  confidence. `compile_workflow` passes the connector index in, so the error
  becomes "`echo` is a tool, not a connector — write `ctx.everything.echo(...)`".
  It changes no verdict; a wrong connector was already rejected a stage later.
  Where the tool name is ambiguous across servers, no server is suggested.
- **A decorator on `run` is a lint error.** Models that have met other agent
  libraries write `@workflow`. pyright answered with `"workflow" is not defined`
  and `untyped function decorator obscures type of function`, and neither says
  the line should not be there. Runlace is not a framework you register with, so
  `run-decorated` says so.
- **The "unknown type" cascade is dropped file-wide, not line by line.** A
  misspelled connector on line 9 makes the variables on lines 10 and 13 unknown
  too. Filtering only the line that already carried a real error left those
  standing, and a small model fixes the line it was shown rather than the one
  that caused it. When the unknowns are all there is, they are the genuine case
  — an unannotated accumulator — and every one is kept.

And these in M6:

- **Two new tools, not a fifth compiler stage.** D3 enumerates the compiler as
  lint, typecheck, extract, store, and running a workflow is not compiling it.
  `dry_run_workflow` sits outside the pipeline, so D3 is untouched and a dry run
  can be repeated as often as you like rather than once at creation.
- **A dry run stands side effects in; it does not skip them.** Skipping would
  change the control flow — `if ctx.gmail.send_email(...)` would take the other
  branch — so every side-effecting call still happens and still returns, just
  from the tool's own `outputSchema` rather than from the server. A tool that
  declares no output shape stands in as `None`, which is what D2 already types
  it as; if the workflow then fails, it assumed a shape nobody promised.
- **Stand-ins are dull on purpose.** Empty string, `False`, a one-element list,
  and `1` rather than `0` so a stand-in in a denominator cannot invent a
  `ZeroDivisionError` the real call would never have caused. Objects are filled
  with every declared property, not only the required ones: a real server does
  send its optional keys, and failing on one would be a false alarm.
- **No confirm gate on a dry run.** D6 gates acting on the world, and a dry run
  does not act on the world. Every other gate — inputs, drift, the connectors —
  applies unchanged, except that the servers hosting only stood-in tools are not
  opened, so a workflow can be checked before the server that would send the
  email is even reachable.
- **A dry run is journaled but never counts as the last run.** "When did this
  last run" is a question about the world. The rows are in `runs` with
  `dry_run = 1`, readable and auditable; `list_workflows` skips them.
- **`edit_workflow` refuses an ambiguous match.** Exactly one occurrence, or
  nothing happens. The alternative is an edit that changes the wrong line and
  reports success, which is the one failure mode the compiler cannot catch.
- **An edit inherits the contract it did not mention.** Schemas and description
  carry over from the version being edited; passing one replaces it. Dropping an
  `outputs_schema` entirely still needs `create_workflow` — removing a declared
  contract deserves the whole file in front of you.
- **Every new version is told it has never run.** Compiling is not evidence that
  a workflow works, and an optional verification step nobody is told about is a
  step nobody takes, so the `create_workflow` result names `dry_run_workflow`.
