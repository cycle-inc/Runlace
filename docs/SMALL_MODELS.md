# Small models

Runlace's premise is that a model writes a workflow once and is then out of the
loop: every later run is deterministic Python. That makes the writing the only
expensive step, and it raises the obvious question — does the writing need a
frontier model?

This is what we measured on a laptop, with the numbers you can reproduce.

## The context budget

`get_skill` is the whole briefing. It is sent once, at the start:

| part | characters |
| --- | --- |
| the skill document | 12,282 |
| the generated stubs | 5,119 |
| the connector index and the task | ~2,000 |
| **total** | **~19,400** |

Roughly 5k tokens, against one MCP server with 13 tools. It grows with the
stubs, not with the skill document, so the number that matters for you is how
many servers you connected. A model with an 8k window will not fit the briefing
plus its own answer plus one rejection; 32k is comfortable.

## Reproducing this

```
ollama serve &
ollama pull qwen3:8b

export RUNLACE_HOME=/tmp/runlace-compat/.runlace
uv run runlace init --from tests/fixtures/mcp_m3.json
./scripts/demo_agent.py --model qwen3:8b --rounds 4 --task "..."
```

`demo_agent.py` talks to any OpenAI-compatible endpoint (`--base-url`), so the
same loop runs against a hosted model unchanged. It prints the model's code, the
compiler's verdict, and the round it was accepted on.

## What we measured

qwen3:8b (Q4, on an M-series laptop), temperature 0, three tasks against
`@modelcontextprotocol/server-everything`:

| task | rounds | what round 1 got wrong |
| --- | --- | --- |
| echo a greeting, add two numbers | **2** | no `from runlace_types import Ctx, Output` |
| the same, reading three declared inputs | **2** | the same missing import |
| a read, a conditional side effect, an `outputs_schema` | **not accepted in 4** | see below |

Each round is one model call plus one `create_workflow`. On this laptop a round
of a thinking model is minutes, so the loop is slow — but it is slow once, and
the workflow it produces runs in milliseconds forever after.

The first two are the shape most workflows have, and both converge on round two
on the same mistake: qwen3:8b writes `def run(ctx: Ctx) -> Output:` and forgets
the import above it. That used to reach pyright and come back as nine
`type of X is unknown` diagnostics; it is now two lint errors naming the line,
and the model fixes it in one round every time.

The third did not converge, and the reason is worth being precise about: the
Python it wrote on round 4 was correct. What it could not do was put that Python
inside a JSON string — it emitted literal backslash-n instead of escaping the
newlines, so the file arrived as one line and lint rejected it as a syntax
error. Three of its four rounds died there. This is a JSON-encoding failure at
8B, not a Runlace one, and it is the argument for having the model call
`create_workflow` as a real tool with structured arguments rather than asking it
to hand-serialise a code block.

### The floor

phi3 (3.8B) does not clear it. On the easiest of the three tasks it spent one
round answering with something that was not a JSON object, one round on a file
with no top-level `run` at all, and one round on arguments `create_workflow`
refused outright. The briefing is not the problem — it fits. The problem is that
"write one function with this exact signature, these imports and nothing else"
is a constraint-following task, and at that size the constraints get dropped.

Somewhere between 4B and 8B is where the compiler stops being an argument the
model loses and starts being a checklist it can work through.

## What made the difference

Every one of these came out of watching a rejected run, not out of design:

- **Send back the hint, not just the message.** A naive harness feeds the model
  `line 9: Cannot access attribute "everthing"` and nothing else. The model
  rewrites the same mistake. `CompileError` carries a `hint` saying what to
  write instead, and `demo_agent.feedback` sends it; that is the single change
  with the largest effect on how many rounds the loop takes.
- **One error, not nine.** A missing `from runlace_types import Ctx` used to
  reach pyright and come back as nine `type of X is unknown` diagnostics, none
  of them naming the import. It is now one lint error, `bad-ctx-annotation`,
  with the line to add.
- **Name the actual mistake.** `ctx.echo(...)` used to be answered with "write
  `ctx.echo.<tool>(...)`" — the same mistake, one level deeper, suggested
  confidently. The lint now knows which connector each tool lives on and says
  "`echo` is a tool, not a connector — write `ctx.everything.echo(...)`".
- **Reject the decorator.** Models that have seen other agent libraries write
  `@workflow` above `run`. pyright answered with `"workflow" is not defined` and
  `untyped function decorator obscures type of function`, neither of which says
  the line should not be there. `run-decorated` says it.
- **Drop the cascade file-wide.** One misspelled connector on line 9 makes the
  variables on lines 10 and 13 unknown too. Filtering the noise only on the line
  that already had a real error left those two standing, and a small model fixes
  the line it was shown rather than the line that caused it.

The pattern behind all five: the compiler was already correct, and the model was
failing on legibility. Rejecting well is a feature, not error handling.

## What still costs rounds

- **Enums in the stubs.** A parameter typed
  `Literal['New York', 'Chicago', 'Los Angeles']` has to be matched exactly, and
  an `inputs_schema` that feeds it needs the same `enum`. Models declare
  `{"type": "string"}` and get a `reportArgumentType` back.
- **Invariance.** Building a list of results in a variable and returning it
  fails against a `TypedDict` `Output`; building it inside the `return` passes.
  There is a specific hint for this, because the generic one sends the model
  rewriting a schema that was already right.
- **`outputs_schema` at all.** Declaring one is the task where every model we
  tried needed the most rounds: it has to keep the schema, the annotation and
  the returned dict in agreement, and a mistake in any of the three reads as a
  mistake in the other two. qwen3:8b's first attempt declared its own
  `Output: TypeAlias = dict[str, object]`, which lint refuses — a local `Output`
  means pyright checks the return against a shape nobody agreed to.
- **Serialising code into JSON.** Not Runlace's problem, but it is what actually
  ended the hardest run. If you can, give the model `create_workflow` as a tool
  and let the client encode the arguments.

## If you are wiring your own loop

Two things, both cheap:

1. Pass `errors[].hint` back to the model verbatim. It is written for it.
2. Do not retry more than three or four times. Past that the model is cycling,
   and the fix is a smaller task or a bigger model — not another round.
