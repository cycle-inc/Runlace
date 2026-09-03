# Runlace

Deterministic, replayable workflows over your MCP servers. An LLM writes a
workflow once; afterwards it runs with no model in the loop.

See `SPEC.md` for the full design. It is committed verbatim and still uses the
working name `harness` throughout; everything in this repo has since been
renamed to Runlace, including the on-disk names D2 and D10 spell out.

**This repo currently implements M1, M2 and M3.**

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
tool name, maps `from_` back to `from`, performs the real call, and writes a
`steps` row before answering. The child's stdout is pointed at stderr and its
stdin at `/dev/null` first, so a stray `print` cannot corrupt the protocol.

Every attempt is journaled, refusals included — `runs` and `steps` are the
audit log, the debug trace and the foundation for v2 resume, so a refused run
still gets a `run_id` you can show a human.

### Not in M3

`runlace sync` and the full SKILL.md are M4; `get_skill` still returns an M2
primer. Resume, scheduling and streaming progress are v2.

## Development

```
uv sync
uv run pytest                    # full suite
uv run pytest -m 'not needs_npx' # skip the tests that launch a real MCP server
uv run pyright                   # Runlace's own source and tests
./scripts/m1_acceptance.sh       # the M1 acceptance criterion, end to end
./scripts/m2_acceptance.sh       # the M2 acceptance criteria, end to end
./scripts/m3_acceptance.sh       # the M3 acceptance criterion, end to end
```

Tests set `RUNLACE_HOME` to a temporary directory, so they never touch your
real `~/.runlace`.

### Decisions this implementation had to make

Two things M1 needs that `SPEC.md` does not pin down:

- **`connectors` and `tools` tables.** The spec's SQL block defines only the
  four workflow tables, but D10 puts everything except workflow code in SQLite
  and M1 has to persist discovery. These two tables are additive; the four
  documented ones are unchanged.
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
  the text block, or the list of them. An `isError` result raises inside the
  workflow, which may catch it; the step is journaled as an error either way.
- **Sessions are opened up front**, one per connector the workflow uses, before
  any workflow code runs. A server that is down fails the run before the first
  side effect rather than halfway through.
