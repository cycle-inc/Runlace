"""The workflow the M10 acceptance authors, and the backend that misbehaves.

The workflow is deliberately half tool and half judgement: it reads a real
temperature off a real MCP server, then asks the model something no tool can
answer -- whether that is coat weather. Everything the milestone claims is
visible in that one shape: the read stays deterministic, the AI step is
journaled next to it, and only the second half changes when the model changes.

`BadBackend` is the other half of the acceptance. A real model cannot be made
to answer wrongly on demand, so the "retried once, then fails readably" claim is
checked against a backend that is OpenAI-shaped, local, and always answers with
the wrong shape -- and counts how many times it was asked.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from _mcp import call as call  # re-exported: the steps import everything from here

CITIES = ["New York", "Chicago", "Los Angeles"]

CODE = """\
from runlace_types import Ctx, Output


def run(ctx: Ctx) -> Output:
    \"\"\"Read a city's weather, then let the model say what to do about it.\"\"\"
    here = ctx.everything.get_structured_content(location=ctx.inputs["city"])

    verdict = ctx.ai(
        system=(
            "You are a weather desk. Answer with JSON only, no prose. "
            "`coat` is true when someone going out now should take one."
        ),
        user=f"{ctx.inputs['city']}: {here['temperature']} degrees Celsius.",
        schema={
            "type": "object",
            "properties": {
                "advice": {"type": "string"},
                "coat": {"type": "boolean"},
            },
            "required": ["advice", "coat"],
        },
    )

    return {
        "city": ctx.inputs["city"],
        "temperature": here["temperature"],
        "advice": verdict["advice"],
        "coat": verdict["coat"],
    }
"""

INPUTS: dict[str, Any] = {
    "type": "object",
    "properties": {
        # The tool's own parameter is an enum, so the input has to be one too.
        "city": {"type": "string", "enum": CITIES},
    },
    "required": ["city"],
}

OUTPUTS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "city": {"type": "string"},
        "temperature": {"type": "number"},
        "advice": {"type": "string"},
        "coat": {"type": "boolean"},
    },
    "required": ["city", "temperature", "advice", "coat"],
}

CHICAGO: dict[str, Any] = {"city": "Chicago"}

# Somewhere in TEST-NET-3, which is reserved for documentation and routed
# nowhere. If a dry run against a remote model sent anything, it would hang
# until the timeout instead of answering in a second.
NOWHERE = "http://203.0.113.7:11434/v1"


class BadBackend:
    """An OpenAI-shaped endpoint that always answers with the wrong shape.

    Local, so the gates treat it as a read and the run needs no confirming --
    what is being checked here is the retry, not the gate.
    """

    def __init__(self) -> None:
        self.asked = 0
        self.lock = threading.Lock()
        backend = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                with backend.lock:
                    backend.asked += 1
                # `coat` is missing and `advice` is a number: two ways to be
                # wrong, so the message the step fails with has something to say.
                body = json.dumps(
                    {
                        "choices": [
                            {"message": {"content": json.dumps({"advice": 12})}}
                        ],
                        "usage": {"prompt_tokens": 20, "completion_tokens": 5},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                """Quiet: the acceptance prints its own narration."""

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def __enter__(self) -> BadBackend:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
