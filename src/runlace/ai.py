"""Talking to the configured model, and nothing else.

One request shape -- ``POST {base_url}/chat/completions`` -- because Ollama,
LiteLLM, OpenRouter, vLLM, llama.cpp and the commercial APIs all speak it. No
provider SDKs and no agent framework: a base URL, a model name and an optional
key cover every backend a user plausibly has, and LiteLLM proxies the rest.

:func:`complete` is transport. :func:`answer` is the step a workflow actually
takes: one question, structured output validated locally against the schema, and
a single retry that shows the model what was wrong with its first attempt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx2

from .model import Model
from .validation import validate

# How much of an error body to quote back. Enough to see "model not found" or a
# provider's JSON error, short enough not to paste a stack trace into a journal.
_BODY_TAIL = 400


class AiFailed(Exception):
    """The model could not be reached, or refused, or answered with nothing."""


@dataclass(frozen=True)
class Completion:
    """One answer, plus what it cost.

    Token counts are optional because not every backend reports usage, and a
    missing count must not be confused with a free call.
    """

    text: str
    tokens_in: int | None = None
    tokens_out: int | None = None


def _payload(
    model: Model, messages: list[dict[str, str]], schema: dict[str, Any] | None
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model.model,
        "messages": messages,
        "stream": False,
    }
    if schema is not None:
        # Sent when the backend understands it, validated locally either way:
        # "the provider says it supports structured output" is not evidence.
        # `strict` is deliberately not set -- it makes OpenAI reject any schema
        # that allows extra properties, which is most schemas people write.
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "output", "schema": schema},
        }
    return body


def _headers(model: Model) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if model.api_key:
        headers["Authorization"] = f"Bearer {model.api_key}"
    return headers


async def complete(
    model: Model,
    messages: list[dict[str, str]],
    *,
    schema: dict[str, Any] | None = None,
) -> Completion:
    """Send one conversation and return what came back.

    ``model`` must already be resolved -- ``${VAR}`` expansion happens where the
    config is read, not here.
    """
    async with httpx2.AsyncClient(timeout=model.timeout) as client:
        response = await _post(client, model, messages, schema)
        if response.status_code >= 400 and schema is not None:
            # Some backends reject `response_format` outright rather than
            # ignoring it. Local validation is the real guarantee, so drop the
            # hint and ask again in plain text instead of failing the step.
            response = await _post(client, model, messages, None)
        if response.status_code >= 400:
            raise AiFailed(
                f"the model at {model.base_url} answered "
                f"{response.status_code}: {response.text[:_BODY_TAIL].strip()}"
            )
        return _read(response.json(), model)


async def _post(
    client: httpx2.AsyncClient,
    model: Model,
    messages: list[dict[str, str]],
    schema: dict[str, Any] | None,
) -> httpx2.Response:
    try:
        return await client.post(
            model.completions_url,
            json=_payload(model, messages, schema),
            headers=_headers(model),
        )
    except httpx2.TimeoutException as exc:
        raise AiFailed(
            f"the model at {model.base_url} did not answer within "
            f"{model.timeout:.0f}s"
        ) from exc
    except httpx2.HTTPError as exc:
        raise AiFailed(
            f"could not reach the model at {model.base_url}: {exc}. "
            "Check that it is running and that `runlace model show` points at it."
        ) from exc


def _read(data: Any, model: Model) -> Completion:
    if not isinstance(data, dict):
        raise AiFailed(f"the model at {model.base_url} answered something that is not JSON")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise AiFailed(f"the model at {model.base_url} answered with no choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise AiFailed(
            f"the model at {model.base_url} answered with empty content "
            f"(model `{model.model}`)"
        )
    usage = data.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    return Completion(
        text=content,
        tokens_in=_count(usage.get("prompt_tokens")),
        tokens_out=_count(usage.get("completion_tokens")),
    )


def _count(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None


# -- one AI step -----------------------------------------------------------


@dataclass(frozen=True)
class Answer:
    """What one `ctx.ai(...)` call produced: the value, and what it cost.

    ``value`` is a validated ``dict`` when a schema was given and the raw
    string when it was not, which is exactly what the stub promises.
    """

    value: Any
    tokens_in: int | None = None
    tokens_out: int | None = None


async def answer(
    model: Model,
    *,
    system: str,
    user: str,
    schema: dict[str, Any] | None = None,
) -> Answer:
    """Ask once, and if the shape is wrong, ask once more and then give up.

    One retry rather than none, because a model that fumbles JSON on the first
    pass usually fixes it when told what was wrong -- and one rather than
    several, because a workflow silently spending four calls on one step is the
    behaviour people who avoid agent frameworks are avoiding.
    """
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    first = await complete(model, messages, schema=schema)
    if schema is None:
        return Answer(first.text, first.tokens_in, first.tokens_out)

    value, problem = _structured(first.text, schema)
    if problem is None:
        return Answer(value, first.tokens_in, first.tokens_out)

    second = await complete(
        model,
        [
            *messages,
            {"role": "assistant", "content": first.text},
            {
                "role": "user",
                "content": f"That answer was rejected: {problem}. Answer again "
                f"with JSON matching the schema exactly, and nothing else -- no "
                f"prose, no code fence.",
            },
        ],
        schema=schema,
    )
    # Both attempts are charged for, so both are counted.
    tokens_in = _add(first.tokens_in, second.tokens_in)
    tokens_out = _add(first.tokens_out, second.tokens_out)

    value, problem = _structured(second.text, schema)
    if problem is not None:
        raise AiFailed(
            f"the model did not answer with the shape this step asked for, "
            f"twice. Second attempt: {problem}"
        )
    return Answer(value, tokens_in, tokens_out)


def _structured(text: str, schema: dict[str, Any]) -> tuple[Any, str | None]:
    """Parse and validate one answer. Returns ``(value, what is wrong)``."""
    try:
        value = json.loads(_unfence(text))
    except json.JSONDecodeError as exc:
        return None, f"it is not JSON ({exc.msg})"
    errors = validate(schema, value)
    if errors:
        return None, "; ".join(str(e) for e in errors)
    return value, None


def _unfence(text: str) -> str:
    """Drop a ```json fence. Models add one however firmly they are told not to."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    body = stripped[3:]
    if body.lower().startswith("json"):
        body = body[4:]
    return body.rsplit("```", 1)[0].strip()


def _add(left: int | None, right: int | None) -> int | None:
    if left is None and right is None:
        return None
    return (left or 0) + (right or 0)
