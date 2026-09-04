"""A scriptable OpenAI-compatible chat-completions server.

Runlace speaks one request shape to every backend, so the honest way to test it
is over a real socket rather than by monkeypatching the HTTP client. This
answers `POST /v1/chat/completions` from a queue of replies written by the test,
and records every request it received so the test can assert what went out.

Run as: python model_server.py <port> <state-dir>

  <state-dir>/replies.json    list of replies, served in order, last one repeats
  <state-dir>/requests.jsonl  written here, one JSON body per line

A reply is either `{"content": "...", "usage": {...}}` or
`{"status": 400, "body": "..."}`.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

STATE = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(".")
REPLIES = STATE / "replies.json"
REQUESTS = STATE / "requests.jsonl"


def next_reply(served: int) -> dict[str, object]:
    try:
        replies = json.loads(REPLIES.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        replies = []
    if not replies:
        return {"content": "ok"}
    return replies[min(served, len(replies) - 1)]


class Handler(BaseHTTPRequestHandler):
    served = 0

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8")
        with REQUESTS.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"path": self.path, "authorization":
                            self.headers.get("Authorization"), "body": raw})
                + "\n"
            )

        reply = next_reply(Handler.served)
        Handler.served += 1

        status = int(reply.get("status", 200))  # type: ignore[arg-type]
        if status >= 400:
            self.send(status, str(reply.get("body", "nope")).encode("utf-8"))
            return

        body = {
            "choices": [{"message": {"role": "assistant",
                                     "content": reply.get("content", "ok")}}],
            "usage": reply.get("usage", {"prompt_tokens": 11, "completion_tokens": 7}),
        }
        self.send(200, json.dumps(body).encode("utf-8"))

    def send(self, status: int, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        """Quiet: the test reads requests.jsonl, not stderr."""


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
