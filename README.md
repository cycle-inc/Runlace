# Runlace

Deterministic, replayable workflows over your MCP servers. An LLM writes a
workflow once; afterwards it runs with no model in the loop.

See `SPEC.md` for the full design. It is committed verbatim and still uses the
working name `harness` throughout; everything in this repo has since been
renamed to Runlace, including the on-disk names D2 and D10 spell out.

**This repo currently implements M1, M2, M3 and M4.**

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
| `run_workflow` | execute one, with no model in the loop (M3) |

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

`run_workflow` executes a stored version. No model is involved: it just runs.

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
Adding a fifth is a change to a locked decision, so it needs to be either an
explicit extension of D3 or a separate `dry_run_workflow` tool outside the
compiler. That call has not been made.

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

`runlace sync` as a command; the dry run described above. Resume, scheduling and
streaming progress are v2.

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
