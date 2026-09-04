#!/usr/bin/env -S uv run python
"""Let a local model write the workflow, and watch the compiler argue with it.

`demo.sh` shows the loop with the workflow already written. This shows the part
that a model actually does: read `get_skill`, write Python, get rejected, fix it.
It talks to any OpenAI-compatible endpoint, and defaults to Ollama on localhost
so a 7B model on a laptop is the intended audience -- see docs/SMALL_MODELS.md.

    ollama serve &
    ollama pull qwen3:8b
    ./scripts/demo_agent.py --task "Echo a greeting and add two numbers"

Nothing here is imported by the package. It is a demo, and a way to reproduce
the small-model numbers in the docs.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from typing import Any

import httpx2
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from _mcp import call
from runlace.paths import paths as runlace_paths
from runlace.server import build_server

DEFAULT_TASK = "Echo a greeting, add two numbers, and return both."

SYSTEM = """\
You write Runlace workflows. Read the skill document and the connector index
below, then answer with one JSON object and nothing else:

  {"name": ..., "description": ..., "code": ...,
   "inputs_schema": ..., "outputs_schema": ...}

`inputs_schema` is always a JSON Schema object; a workflow that reads nothing
from `ctx.inputs` declares `{"type": "object", "properties": {}}`.
`outputs_schema` is a JSON Schema object, or null to declare no output shape.

`code` is a single Python string containing the whole workflow file. Follow the
skill document exactly: it lists what is forbidden and why. Do not explain
yourself, do not wrap the JSON in a code fence.
"""

JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def prompt_for(server: MCPServer, skill: dict[str, Any], task: str) -> str:
    """SKILL.md, the live connector index, the signatures, and the job.

    The server splits these in two -- ``get_skill`` for the index, ``get_tools``
    for the signatures of the few tools an agent picked. This harness is a
    single prompt to a model that is not calling tools, so it asks for all of
    them and pastes them in. That is the expensive spelling on purpose: it is
    also the one that measures what a small model can hold.
    """
    index = "\n".join(
        f"{tool['risk']:<12}{tool['call']}\n    {tool['description'] or ''}"
        for connector in skill["connectors"]
        for tool in connector["tools"]
    )
    stubs = "\n\n".join(
        f"# {connector['connector']}\n{call(server, 'get_tools', connector=connector['connector'])['types']}"
        for connector in skill["connectors"]
    )
    return (
        f"{skill['skill']}\n\n"
        f"# Your connectors\n\n{index}\n\n"
        f"# The generated stubs, which pyright will check you against\n\n{stubs}\n\n"
        f"# Your task\n\n{task}\n"
    )


def ask(
    base_url: str,
    model: str,
    messages: list[dict[str, str]],
    timeout: float,
    api_key: str | None = None,
) -> str:
    # Ollama needs no key. A hosted endpoint does, and it is read from the
    # environment rather than passed on the command line: an argument would be
    # in the shell history and in `ps`.
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    response = httpx2.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers=headers,
        json={
            "model": model,
            "messages": messages,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        },
        timeout=timeout,
    )
    response.raise_for_status()
    return str(response.json()["choices"][0]["message"]["content"])


def parse(answer: str) -> dict[str, Any] | None:
    """Small models bury the JSON in prose often enough to be worth handling."""
    match = JSON_OBJECT.search(answer)
    if match is None:
        return None
    try:
        value = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def feedback(result: dict[str, Any]) -> str:
    """What the model gets told after a rejection.

    The hint is the half that matters. Sending back only `line: message` -- which
    is what a naive harness does -- measurably costs rounds: the model rewrites
    the same mistake because nothing told it what to do instead.
    """
    lines = [f"The compiler rejected this at the `{result['stage']}` stage:"]
    for error in result["errors"]:
        where = f"line {error['line']}: " if error.get("line") else ""
        lines.append(f"  {where}{error['message']}")
        if error.get("hint"):
            lines.append(f"    hint: {error['hint']}")
    lines.append("Answer with the corrected JSON object, and nothing else.")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:11434/v1")
    parser.add_argument("--model", default="qwen3:8b")
    parser.add_argument("--task", default=DEFAULT_TASK)
    parser.add_argument("--name", default="demo-agent")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument(
        "--api-key-env",
        help="Name of the environment variable holding the endpoint's API key. "
        "Unset for Ollama. The value is never printed.",
    )
    args = parser.parse_args()
    api_key = os.environ.get(args.api_key_env) if args.api_key_env else None
    if args.api_key_env and not api_key:
        print(f"${args.api_key_env} is not set", file=sys.stderr)
        return 2
    # One INFO line per request, in a box, in the middle of the model's code.
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    # A round on a laptop takes minutes. Piped to a file, the default block
    # buffering means nothing appears until the run ends and the whole thing
    # looks hung.
    sys.stdout.reconfigure(line_buffering=True)

    server = build_server(runlace_paths())
    skill = call(server, "get_skill")
    prompt = prompt_for(server, skill, args.task)
    print(f"context: {len(prompt):,} characters")

    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": prompt},
    ]

    for round_number in range(1, args.rounds + 1):
        print(f"\n== round {round_number}: asking {args.model}")
        try:
            answer = ask(args.base_url, args.model, messages, args.timeout, api_key)
        except httpx2.HTTPError as error:
            print(f"cannot reach {args.base_url}: {error}", file=sys.stderr)
            return 2

        proposal = parse(answer)
        if proposal is None or not isinstance(proposal.get("code"), str):
            print(answer[:600])
            messages += [
                {"role": "assistant", "content": answer},
                {"role": "user", "content": "That was not one JSON object with a "
                                            "string `code` field. Answer again."},
            ]
            continue

        print(proposal["code"])
        try:
            result = call(
                server,
                "create_workflow",
                name=args.name,
                description=str(proposal.get("description") or args.task),
                code=proposal["code"],
                inputs_schema=proposal.get("inputs_schema"),
                outputs_schema=proposal.get("outputs_schema"),
            )
        except ToolError as error:
            # The arguments themselves were wrong -- a null `inputs_schema`, a
            # name with a space in it -- so there is no compiler verdict to
            # relay. A real client sees the same message; give the model its
            # round back rather than crashing the loop.
            print(f"\nrejected before compiling: {error}")
            messages += [
                {"role": "assistant", "content": answer},
                {"role": "user", "content": f"`create_workflow` refused the "
                                            f"arguments: {error}\nAnswer with "
                                            f"the corrected JSON object."},
            ]
            continue
        if result["ok"]:
            print(f"\naccepted on round {round_number}: version {result['version']}")
            for tool in result["tools_used"]:
                print(f"  {tool['risk']:<12}{tool['connector']}.{tool['tool']}")
            return 0

        told = feedback(result)
        print(f"\n{told}")
        messages += [
            {"role": "assistant", "content": answer},
            {"role": "user", "content": told},
        ]

    print(f"\nstill rejected after {args.rounds} rounds.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
