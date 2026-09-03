# Runlace

Deterministic, replayable workflows over your MCP servers. An LLM writes a
workflow once; afterwards it runs with no model in the loop.

See `SPEC.md` for the full design. It is committed verbatim and still uses the
working name `harness` throughout; everything in this repo has since been
renamed to Runlace, including the on-disk names D2 and D10 spell out.

**This repo currently implements M1 and M2.**

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

`runlace serve` starts an MCP server that any host can add. It exposes four
tools:

| tool | what it does |
| --- | --- |
| `get_skill` | how to write a workflow, plus every connector, tool and stub |
| `create_workflow` | compile a workflow and, if it passes, store a version |
| `list_workflows` | one line per workflow |
| `get_workflow` | the code, the schemas, the pinned tools, the version list |

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

### Not in M2

`run_workflow`, the runner and the confirm gate are M3; `runlace sync` and the
full SKILL.md are M4. They are absent rather than stubbed out — `get_skill`
currently returns an M2 primer.

## Development

```
uv sync
uv run pytest                    # full suite
uv run pytest -m 'not needs_npx' # skip the tests that launch a real MCP server
./scripts/m1_acceptance.sh       # the M1 acceptance criterion, end to end
./scripts/m2_acceptance.sh       # the M2 acceptance criteria, end to end
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
