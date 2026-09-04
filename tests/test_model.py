"""The model config and the one client that talks to every backend.

The client is exercised over a real socket against a scriptable
chat-completions server rather than a patched HTTP client: what is being proved
is the shape of the request Runlace puts on the wire, and a mock would prove
only that the mock matches the code.
"""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from runlace.ai import AiFailed, complete
from runlace.config import MissingEnvVars
from runlace.model import (
    DEFAULT_TIMEOUT,
    Model,
    read_model,
    unset_references,
    write_model,
)

SERVER = Path(__file__).parent / "fixtures" / "model_server.py"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Backend:
    """A running fake backend, and the two files the test drives it with."""

    def __init__(self, state: Path, port: int) -> None:
        self.state = state
        self.base_url = f"http://127.0.0.1:{port}/v1"

    def replies(self, *replies: dict[str, Any]) -> None:
        (self.state / "replies.json").write_text(json.dumps(list(replies)))

    def requests(self) -> list[dict[str, Any]]:
        path = self.state / "requests.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line]

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r["body"]) for r in self.requests()]

    def model(self, name: str = "test-model", **kwargs: Any) -> Model:
        return Model(base_url=self.base_url, model=name, **kwargs)


@pytest.fixture
def backend(tmp_path: Path) -> Iterator[Backend]:
    state = tmp_path / "backend"
    state.mkdir()
    port = free_port()
    process = subprocess.Popen(
        [sys.executable, str(SERVER), str(port), str(state)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail("the fake model server exited during startup")
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.05)
        else:
            pytest.fail("the fake model server never started listening")
        yield Backend(state, port)
    finally:
        process.terminate()
        process.wait(timeout=10)


def ask(model: Model, **kwargs: Any) -> Any:
    return asyncio.run(complete(model, [{"role": "user", "content": "hi"}], **kwargs))


# -- the config file ---


def test_a_model_round_trips_through_its_file(tmp_path: Path) -> None:
    path = tmp_path / "model.json"
    write_model(path, Model(base_url="http://x/v1", model="m", timeout=42.0))

    assert read_model(path) == Model(base_url="http://x/v1", model="m", timeout=42.0)


def test_no_file_means_no_model_rather_than_an_error(tmp_path: Path) -> None:
    """Every caller is a gate wanting to say something useful, not to crash."""
    assert read_model(tmp_path / "absent.json") is None
    (tmp_path / "junk.json").write_text("{not json")
    assert read_model(tmp_path / "junk.json") is None
    (tmp_path / "half.json").write_text('{"base_url": "http://x/v1"}')
    assert read_model(tmp_path / "half.json") is None


def test_the_key_stays_a_reference_on_disk(tmp_path: Path) -> None:
    path = tmp_path / "model.json"
    write_model(path, Model(base_url="http://x/v1", model="m", api_key="${SOME_KEY}"))

    assert "${SOME_KEY}" in path.read_text()

    resolved = read_model(path)
    assert resolved is not None
    assert resolved.resolved({"SOME_KEY": "sk-real"}).api_key == "sk-real"
    with pytest.raises(MissingEnvVars):
        resolved.resolved({})


def test_the_variables_a_model_needs_can_be_checked_without_calling_it() -> None:
    """`serve` asks this at startup, so it cannot cost a request."""
    model = Model(base_url="${AI_HOST}/v1", model="m", api_key="${SOME_KEY}")

    assert unset_references(model, {}) == ["AI_HOST", "SOME_KEY"]
    assert unset_references(model, {"AI_HOST": "http://x", "SOME_KEY": "k"}) == []
    assert unset_references(Model(base_url="http://x/v1", model="m"), {}) == []


def test_a_default_timeout_is_not_written_out(tmp_path: Path) -> None:
    """The file should read as the two things a user chose, not as a dump."""
    path = tmp_path / "model.json"
    write_model(path, Model(base_url="http://x/v1", model="m"))

    assert json.loads(path.read_text()) == {
        "version": 1,
        "base_url": "http://x/v1",
        "model": "m",
    }
    assert read_model(path) is not None
    assert read_model(path).timeout == DEFAULT_TIMEOUT  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "base_url,local",
    [
        ("http://localhost:11434/v1", True),
        ("http://127.0.0.1:4000/v1", True),
        ("http://[::1]:8080/v1", True),
        ("https://openrouter.ai/api/v1", False),
        ("https://api.openai.com/v1", False),
        ("http://ollama.internal:11434/v1", False),
    ],
)
def test_distance_is_read_off_the_base_url(base_url: str, local: bool) -> None:
    """Whether an AI step is a read or a side effect turns on exactly this."""
    assert Model(base_url=base_url, model="m").is_local is local


# -- the client ---


def test_one_completion_comes_back_with_its_token_counts(backend: Backend) -> None:
    backend.replies(
        {"content": "ready", "usage": {"prompt_tokens": 31, "completion_tokens": 4}}
    )

    answer = ask(backend.model())

    assert answer.text == "ready"
    assert (answer.tokens_in, answer.tokens_out) == (31, 4)


def test_the_request_is_the_openai_shape_every_backend_understands(
    backend: Backend,
) -> None:
    backend.replies({"content": "ok"})
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}

    asyncio.run(
        complete(
            backend.model("qwen3:8b", api_key="sk-test"),
            [{"role": "system", "content": "be terse"}, {"role": "user", "content": "hi"}],
            schema=schema,
        )
    )

    sent = backend.requests()[0]
    assert sent["path"] == "/v1/chat/completions"
    assert sent["authorization"] == "Bearer sk-test"
    body = json.loads(sent["body"])
    assert body["model"] == "qwen3:8b"
    assert body["stream"] is False
    assert body["messages"][0] == {"role": "system", "content": "be terse"}
    assert body["response_format"]["json_schema"]["schema"] == schema


def test_a_backend_that_rejects_response_format_is_asked_again_without_it(
    backend: Backend,
) -> None:
    """llama.cpp and friends 400 on it. Validation is local anyway, so ask plainly."""
    backend.replies({"status": 400, "body": "unknown field response_format"},
                    {"content": '{"a": 1}'})

    answer = ask(backend.model(), schema={"type": "object"})

    assert answer.text == '{"a": 1}'
    bodies = backend.bodies()
    assert "response_format" in bodies[0]
    assert "response_format" not in bodies[1]


def test_a_refusal_without_a_schema_is_not_retried(backend: Backend) -> None:
    backend.replies({"status": 401, "body": "no credit"})

    with pytest.raises(AiFailed) as exc:
        ask(backend.model())

    assert "401" in str(exc.value)
    assert "no credit" in str(exc.value)
    assert len(backend.requests()) == 1


def test_empty_content_is_a_failure_not_an_empty_answer(backend: Backend) -> None:
    """A workflow branching on "" would take a decision nobody made."""
    backend.replies({"content": "   "})

    with pytest.raises(AiFailed) as exc:
        ask(backend.model())

    assert "empty content" in str(exc.value)


def test_an_unreachable_model_says_where_it_looked() -> None:
    model = Model(base_url=f"http://127.0.0.1:{free_port()}/v1", model="m", timeout=2.0)

    with pytest.raises(AiFailed) as exc:
        ask(model)

    assert model.base_url in str(exc.value)
    assert "runlace model show" in str(exc.value)


def test_missing_usage_reports_unknown_rather_than_zero(backend: Backend) -> None:
    """A backend that does not count tokens has not made the call free."""
    backend.replies({"content": "ok", "usage": {}})

    answer = ask(backend.model())

    assert (answer.tokens_in, answer.tokens_out) == (None, None)
