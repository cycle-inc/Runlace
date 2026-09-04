# Contributing to Runlace

Thanks for looking. This is a young project with a written design, so the
contribution loop is short and the one rule below is worth reading before you
open an editor.

## The rule: `SPEC.md` is the source of truth

`SPEC.md` holds the design and a set of numbered decisions (D1–D10). They are
deliberately locked: they are not revisited or "improved" in passing. If you
believe one is wrong, say so in an issue and make the argument — that is a
conversation worth having, and a pull request that quietly deviates from one is
not, because the whole point of writing them down was to stop relitigating them
one file at a time.

Anything the spec does not pin down is fair game. Those calls are collected at
the end of `README.md` under "Decisions this implementation had to make", with
the reasoning; add to that list when you make a new one.

## Setting up

```
uv sync
uv run pytest                    # the full suite
uv run pytest -m 'not needs_npx' # skip the tests that launch a real MCP server
uv run pyright                   # source and tests
```

Tests set `RUNLACE_HOME` to a temporary directory, so they never touch your real
`~/.runlace`. The ones marked `needs_npx` spawn a real
`@modelcontextprotocol/server-everything` over stdio; they need `npx` on PATH
and they are worth running before you send anything.

The `scripts/m*_acceptance.sh` files are end-to-end checks against live servers,
one per milestone. `scripts/m10_acceptance.sh` additionally needs a running
`ollama serve` with `qwen3:8b` pulled.

## What a change looks like

- **Tests come with it.** Every module has a `tests/test_<module>.py`. An
  acceptance criterion is a passing test, not a claim in a commit message.
- **Boring beats clever.** This code gets read by strangers deciding whether to
  trust it with a token and a `send_email` tool. Prefer the obvious version.
- **Comments explain why, not what.** The existing ones say what a reader could
  not have worked out: why a stand-in returns `1` and not `0`, why the model
  lives in its own file. Match that density.
- **Typing is not optional.** pyright runs in standard mode over `src` and
  `tests`, and in strict mode over generated stubs and workflow code. Zero
  errors, no `# type: ignore` without a sentence saying why.
- **One thing per pull request.** A refactor bundled with a fix makes the fix
  impossible to review.

Commit messages here are a subject line under ~60 characters followed by a body
that explains the reasoning. Look at `git log` for the register.

## Especially useful

- **An MCP server whose stubs come out wrong.** The generator has been tried
  against a handful of servers; every server spells its schemas a little
  differently and only real ones find the gaps. Include the server and the
  `tools/list` output if you can share it.
- **A workflow the compiler rejected but should not have**, or accepted and
  should not have. Both are lint or typecheck bugs, and both are reproducible in
  a test in a few lines.
- **Anything in `SKILL.md` that misled an agent.** That file is documentation the
  compiler has to agree with; a pattern it teaches that gets rejected is a bug in
  one of the two, and `tests/test_skill_examples.py` exists to catch exactly
  that.

## Reporting a vulnerability

Not through an issue — see `SECURITY.md`.
