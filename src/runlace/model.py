"""Which model `ctx.ai` calls, and where it lives.

One backend per machine, declared once and stored in ``~/.runlace/model.json``.
Workflow code never names a model: the agent that writes a workflow is not the
party who pays for inference or answers for where the data went, and a workflow
that hard-codes a model name breaks the day its owner switches to a local one.

The file is its own document rather than a section of ``config.json`` because
writing that one replaces it whole -- every caller that does not know about a
model section would erase one. An inference backend is also not an MCP
connector, and pretending otherwise would mean faking a ``tools/list``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .config import MissingEnvVars, _expand

MODEL_VERSION = 1

# Ollama's OpenAI-compatible endpoint. The default because it is the one setup
# that costs nothing, sends nothing anywhere, and needs no key -- so `runlace
# model set qwen3:8b` is the whole command for the case we want people to try.
DEFAULT_BASE_URL = "http://localhost:11434/v1"

# Long, because a local 8B model on a laptop with no GPU genuinely takes this
# long to answer, and a timeout that fires on a working setup is worse than a
# slow one.
DEFAULT_TIMEOUT = 120.0

# A model reachable only on this machine has sent nothing anywhere, so an AI
# step against it is a read. Everything else is a side effect: handing the
# user's data to a third party is an effect on the world, and the one a
# developer most wants a gate on.
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


class NoModelConfigured(Exception):
    """`ctx.ai` was reached with no backend set up."""

    def __init__(self) -> None:
        super().__init__(
            "no model is configured; run `runlace model set` to choose one "
            "(a local Ollama, a LiteLLM proxy, OpenRouter, ...)"
        )


@dataclass(frozen=True)
class Model:
    """An OpenAI-shaped chat completions endpoint.

    Ollama, LiteLLM, OpenRouter, vLLM, llama.cpp and the commercial APIs all
    speak ``POST {base_url}/chat/completions``, so this covers every backend a
    user plausibly has and the rest are proxied by LiteLLM.
    """

    base_url: str
    model: str
    api_key: str | None = None
    timeout: float = DEFAULT_TIMEOUT

    @property
    def is_local(self) -> bool:
        """True when the endpoint is on this machine and nothing leaves it."""
        host = urlparse(self.base_url).hostname
        return host is not None and host.lower() in LOCAL_HOSTS

    @property
    def completions_url(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "version": MODEL_VERSION,
            "base_url": self.base_url,
            "model": self.model,
        }
        if self.api_key:
            data["api_key"] = self.api_key
        if self.timeout != DEFAULT_TIMEOUT:
            data["timeout"] = self.timeout
        return data

    def resolved(self, environ: Mapping[str, str] | None = None) -> Model:
        """This model with every ``${VAR}`` replaced by its value.

        Same rule as connectors: the file keeps the reference, the environment
        holds the secret, and a key is never written to disk.
        """
        env = os.environ if environ is None else environ
        missing: list[str] = []
        expanded = replace(
            self,
            base_url=_expand(self.base_url, env, missing),
            api_key=_expand(self.api_key, env, missing) if self.api_key else None,
        )
        if missing:
            raise MissingEnvVars(missing)
        return expanded


def unset_references(model: Model, environ: Mapping[str, str] | None = None) -> list[str]:
    """The ``${VAR}`` names this model refers to that have no value right now.

    Worth saying at configuration time rather than at call time: a ``ctx.ai``
    step resolves the key when the workflow reaches it, which can be after a
    step that already sent an email. "Not set" is a setup problem and belongs
    where the setup happens.
    """
    try:
        model.resolved(environ)
    except MissingEnvVars as exc:
        return exc.names
    return []


def read_model(path: Path) -> Model | None:
    """The configured model, or None if there is not one.

    A missing or unreadable file means "no model", never an exception: callers
    are gates and compilers that want to say something useful about it.
    """
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict):
        return None
    base_url = doc.get("base_url")
    name = doc.get("model")
    if not isinstance(base_url, str) or not base_url:
        return None
    if not isinstance(name, str) or not name:
        return None
    api_key = doc.get("api_key")
    timeout = doc.get("timeout")
    return Model(
        base_url=base_url,
        model=name,
        api_key=api_key if isinstance(api_key, str) and api_key else None,
        timeout=float(timeout) if isinstance(timeout, (int, float)) else DEFAULT_TIMEOUT,
    )


def write_model(path: Path, model: Model) -> None:
    path.write_text(json.dumps(model.to_json(), indent=2) + "\n", encoding="utf-8")
