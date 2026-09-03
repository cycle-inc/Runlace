# Runlace

Deterministic, replayable workflows over your MCP servers. An LLM writes a
workflow once; afterwards it runs with no model in the loop.

See `SPEC.md` for the full design. It is committed verbatim and still uses the
working name `harness` throughout; everything in this repo has since been
renamed to Runlace, including the on-disk names D2 and D10 spell out.

**This repo currently implements M1 only.**

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

### Not in M1

`runlace serve`, `runlace sync`, `create_workflow`, the runner, SKILL.md and
`policy.yaml` all belong to later milestones and are deliberately absent rather
than stubbed out.

## Development

```
uv sync
uv run pytest                    # full suite
uv run pytest -m 'not needs_npx' # skip the tests that launch a real MCP server
./scripts/m1_acceptance.sh       # the M1 acceptance criterion, end to end
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
