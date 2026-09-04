"""Talking to the configured model, and nothing else.

One request shape -- ``POST {base_url}/chat/completions`` -- because Ollama,
LiteLLM, OpenRouter, vLLM, llama.cpp and the commercial APIs all speak it. No
provider SDKs and no agent framework: a base URL, a model name and an optional
key cover every backend a user plausibly has, and LiteLLM proxies the rest.

This module is transport only. Building the conversation, validating structured
output and retrying a model that ignored its schema belong to the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx2

from .model import Model

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
