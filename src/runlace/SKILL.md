# Writing a Runlace workflow

Runlace turns a piece of Python into a stored, replayable workflow over the MCP
servers this machine is connected to. You write it once, a compiler checks it
against the real tool schemas, and afterwards it runs with no model in the loop:
same code, same tools, same result, journaled every time. Your job is to produce
that code once and get it to compile.

## The loop

1. **`get_skill`** — you are here. Below this document you were sent a
   `connectors` index (every tool on this machine, with its risk) and `stubs`
   (the generated `.pyi` files). Read them: they are the ground truth for what
   you can call, and they are specific to this machine.
2. **`create_workflow`** — send `name`, `description`, `code`, `inputs_schema`,
   and `outputs_schema` if you want the return value checked. `inputs_schema` is
   always an object and is never `null`; a workflow that reads nothing from
   `ctx.inputs` declares `{"type": "object", "properties": {}}`. The code is
   compiled: linted, typechecked with pyright against those stubs, and its tool
   calls extracted and pinned. On failure you get the stage, the line number, an
   error code and a hint. Fix and send again — that is a normal part of the loop,
   not a failure of the task.
3. **`run_workflow`** — execute it. If the workflow touches any side-effecting
   tool, the run is refused until you show the human exactly which tools will act
   and call again with `confirm=True`.

## The calling convention

You never import connectors. You receive `ctx`. Call tools as
`ctx.<connector>.<tool>(**kwargs)` — for example:

```python
ctx.pennylane.list_transactions(from_="2024-01-01", to="2024-01-31")
```

Runlace resolves that call to the real MCP server at run time.

Use static attribute access only. `getattr(ctx, name)` is rejected, and so is
storing `ctx`, a connector or a tool in a variable:

```python
client = ctx.gmail            # ctx-escape
send = ctx.gmail.send_email   # ctx-tool-not-called
```

The tools a workflow uses are read off the source before it ever runs — that is
what makes the confirm gate and the schema pinning possible — so every call has
to be spelled out in full.

Arguments are keyword-only. A JSON key that is a Python reserved word gets a
trailing underscore in the stub (`from` becomes `from_`, `class` becomes
`class_`) and is mapped back to the original name at run time.

Read the `.pyi` excerpt for a connector before calling it. It carries the exact
parameter names, which ones are required, the return type, and the tool's risk
in the docstring.

## The workflow file contract

```python
from runlace_types import Ctx

def run(ctx: Ctx) -> dict[str, object]:
    result = ctx.pennylane.list_transactions(
        from_=ctx.inputs["from"], to=ctx.inputs["to"]
    )
    return {"count": len(result["transactions"])}
```

- Exactly one top-level `def run(ctx)`. `async def run` is rejected — the runner
  shim handles the RPC, so your code is plain synchronous Python.
- Annotate the return type. Use `-> Output` when you declare an
  `outputs_schema` (`Output` is generated from that schema, so pyright checks
  what you return against it); use `-> dict[str, object]` when you do not.
- When `outputs_schema` has an array of objects, build that list *inside* the
  `return` statement. Assembled in a variable first it is inferred as a plain
  `list[dict[...]]`, which pyright will not accept as a list of the generated
  item type — lists are invariant. Example 3 below does it the working way.
- `ctx.inputs` is subscripted with the raw JSON key from your `inputs_schema`:
  `ctx.inputs["from"]`, not `ctx.inputs.from_`. It is a real dict, so
  `ctx.inputs.get("branch", "main")` works for an optional input.
- Helper functions, dataclasses and comprehensions are all fine. Everything
  between the tool calls is ordinary Python running in the subprocess.
- Imports are limited to: `collections`, `dataclasses`, `datetime`, `decimal`,
  `itertools`, `json`, `math`, `re`, `statistics`, `typing` — plus
  `runlace_types`.

### What a tool returns

If the tool declares an `outputSchema`, the stub gives you a `TypedDict` and
pyright checks your field names. If it does not — and many servers declare
none — the stub returns `Any`, and nothing is checked. You are guessing at the
shape, and a wrong guess compiles cleanly and fails at run time.

Two things help:

- **Probe first.** If the same MCP server is connected to your own client, and
  the tool is marked `read_only` in the connector index, call it yourself once
  with realistic arguments and look at what comes back. Then write the workflow
  against the shape you actually saw. Never probe a tool whose risk is
  `side_effect`: calling it has real consequences, and a probe is not something
  the human confirmed.
- **Be defensive where you did not probe.** Prefer `.get(...)` over `[...]` for
  fields you are not sure exist, and check `isinstance(value, list)` before
  iterating something that might be a dict.

Runlace unwraps the MCP envelope for you: a tool whose reply is a single block
of JSON arrives as a dict or a list, not as a string you have to parse. Do not
call `json.loads` on a tool result.

One strict-mode trap comes with `Any`: building a list in a loop over an
unchecked result leaves pyright unable to infer its element type.

```python
names: list[str] = []          # annotate it, or use a comprehension
for repo in result["items"]:
    names.append(str(repo["name"]))
```

## The inputs rule

Inline constants are fine when they define the workflow itself — a fixed board
ID, a URL, a report format. Anything a user might vary between runs — dates,
recipients, amounts, filters, repository names — must be a declared input, with
a sensible default where possible.

Good — the period is an input, so the workflow is still correct next month:

```python
result = ctx.pennylane.list_transactions(
    from_=ctx.inputs["from"], to=ctx.inputs["to"]
)
```

Bad — the workflow has to be recreated in February:

```python
result = ctx.pennylane.list_transactions(from_="2024-01-01", to="2024-01-31")
```

## Forbidden patterns

Each is rejected at lint with the error code shown, a line number and a hint.

| Code | What it catches |
| --- | --- |
| `forbidden-import` | `subprocess`, `os`, `sys`, `socket`, `http`, `urllib`, `requests`, `httpx`, `aiohttp`, `importlib` |
| `import-not-allowed` | anything else off the allowlist above |
| `relative-import` | `from . import x` |
| `stub-submodule-import` | importing inside `runlace_types` instead of from it |
| `forbidden-call` | `open`, `exec`, `eval`, `__import__` |
| `dynamic-attribute-access` | `getattr`, `setattr`, `delattr`, `vars`, `globals`, `locals` |
| `dunder-access` | reaching for `__class__`, `__globals__` and friends |
| `async-not-supported` | `async def`, `await` |
| `missing-run` | no top-level `def run` |
| `bad-run-signature` | `run` takes something other than a single `ctx` |
| `run-decorated` | a decorator on `run` — there is no framework to register with |
| `ctx-rebound` | assigning to `ctx` |
| `ctx-escape` | putting `ctx` or a connector in a variable, list or call argument |
| `ctx-tool-not-called` | referencing a tool without calling it |
| `unknown-connector` | `ctx.<tool>(...)` — the tool's name where the server's belongs |
| `missing-return-annotation` | `def run(ctx):` with no `->` |
| `bad-ctx-annotation` | `ctx` not annotated `Ctx`, or `Ctx` not imported from `runlace_types` |
| `bad-output-annotation` | an `outputs_schema` without `-> Output`, or an `Output` you defined or never imported |
| `syntax-error` | the code does not parse |

There is no escape hatch. A workflow that needs the network gets there through
an MCP tool, or not at all.

## Three complete examples

Each one shows the arguments you pass to `create_workflow` and the code that
goes with them. They compile against the two connectors used throughout this
document; yours will differ — read the connector index.

### 1. Read-only report

No side effects, so `run_workflow` executes it without asking for confirmation.

```json
{
  "inputs_schema": {
    "type": "object",
    "properties": {
      "from": {"type": "string"},
      "to": {"type": "string"}
    },
    "required": ["from", "to"]
  }
}
```

```python
from runlace_types import Ctx


def run(ctx: Ctx) -> dict[str, object]:
    result = ctx.pennylane.list_transactions(
        from_=ctx.inputs["from"], to=ctx.inputs["to"]
    )
    transactions = result["transactions"]
    total = sum(t["amount"] for t in transactions)
    return {
        "count": len(transactions),
        "total": total,
        "average": total / len(transactions) if transactions else 0,
    }
```

### 2. A single side effect

`gmail.send_email` is not annotated read-only, so this workflow is gated: the
first `run_workflow` call comes back with `needs-confirmation` and the list of
tools that will act. Show that list to the human, and only then call again with
`confirm=True`.

```json
{
  "inputs_schema": {
    "type": "object",
    "properties": {
      "email": {"type": "string"},
      "note": {"type": "string", "default": "Nothing to report."}
    },
    "required": ["email"]
  }
}
```

```python
from runlace_types import Ctx


def run(ctx: Ctx) -> dict[str, object]:
    ctx.gmail.send_email(
        to=ctx.inputs["email"],
        subject="Daily note",
        body=ctx.inputs.get("note", "Nothing to report."),
    )
    return {"sent_to": ctx.inputs["email"]}
```

### 3. Multi-step, with an outputs_schema

Read, compute, then act — and declare what comes back, so pyright checks the
return value at compile time and Pydantic validates it again after the run.
`Output` is generated from `outputs_schema`; you do not define it.

```json
{
  "inputs_schema": {
    "type": "object",
    "properties": {
      "from": {"type": "string"},
      "to": {"type": "string"},
      "email": {"type": "string"},
      "threshold": {"type": "number", "default": 1000}
    },
    "required": ["from", "to", "email"]
  },
  "outputs_schema": {
    "type": "object",
    "properties": {
      "count": {"type": "integer"},
      "flagged": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "id": {"type": "string"},
            "amount": {"type": "number"}
          },
          "required": ["id", "amount"]
        }
      },
      "notified": {"type": "boolean"}
    },
    "required": ["count", "flagged", "notified"]
  }
}
```

```python
from runlace_types import Ctx, Output


def run(ctx: Ctx) -> Output:
    result = ctx.pennylane.list_transactions(
        from_=ctx.inputs["from"], to=ctx.inputs["to"]
    )
    transactions = result["transactions"]
    threshold = ctx.inputs.get("threshold", 1000)
    flagged = [t for t in transactions if t["amount"] > threshold]

    if flagged:
        lines = [f"{t['id']}: {t['amount']}" for t in flagged]
        ctx.gmail.send_email(
            to=ctx.inputs["email"],
            subject=f"{len(flagged)} transactions over {threshold}",
            body="\n".join(lines),
        )

    return {
        "count": len(transactions),
        # Built here, not in a variable above: `outputs_schema` gives each item
        # its expected type, which a `list[dict[...]]` inferred elsewhere loses.
        "flagged": [{"id": t["id"], "amount": t["amount"]} for t in flagged],
        "notified": bool(flagged),
    }
```

## When something is refused

Every refusal carries a `code`. The ones worth recognising:

- `needs-confirmation` — the workflow acts. Show the human the `side_effects`
  list, then call `run_workflow` again with `confirm=True`.
- `invalid-inputs` — what you passed does not match the `inputs_schema` the
  workflow declares. `get_workflow` returns that schema.
- `schema-drift` — a tool changed since the workflow compiled. Run
  `runlace init` to re-discover, check what moved, and create a new version.
- `invalid-output` — the workflow ran and its side effects happened, but the
  return value does not match `outputs_schema`. Fix one or the other and create
  a new version.
- `workflow-failed` — the code raised. The line number in `detail` is a line of
  your own code.

A compile failure is not a refusal code but a list of errors, each with a stage
(`lint`, `typecheck`, `extract`), a line and a hint. Read the line number: it is
a line of the code you just sent.
