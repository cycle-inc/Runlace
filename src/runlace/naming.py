"""Turning MCP names into Python names.

MCP tool names are kept verbatim in storage and on the wire (D2). These helpers
only decide how a name is *spelled in the generated stubs*, and every mapping
here is deterministic so the runtime can reverse it.
"""

from __future__ import annotations

import keyword
import re

_NON_IDENT = re.compile(r"[^0-9a-zA-Z_]+")


def is_reserved(name: str) -> bool:
    return keyword.iskeyword(name) or keyword.issoftkeyword(name)


def python_identifier(raw: str) -> str | None:
    """Best-effort conversion of an arbitrary name to a Python identifier.

    Returns ``None`` when the name has nothing usable in it (e.g. ``"---"``).
    """
    cleaned = _NON_IDENT.sub("_", raw).strip("_")
    if not cleaned:
        return None
    if cleaned[0].isdigit():
        cleaned = f"_{cleaned}"
    if is_reserved(cleaned):
        cleaned = f"{cleaned}_"
    return cleaned


def param_name(json_key: str) -> str | None:
    """Map a JSON Schema property name to a keyword-argument name.

    Reserved words get a trailing underscore (``from`` -> ``from_``), which the
    runtime maps back. Keys that are not identifiers at all (``content-type``)
    return ``None``: we refuse to invent a lossy spelling for them.
    """
    if not json_key.isidentifier():
        return None
    if is_reserved(json_key):
        return f"{json_key}_"
    return json_key


def unmap_param_name(param: str, json_keys: list[str]) -> str:
    """Reverse :func:`param_name` against the schema's actual keys."""
    if param in json_keys:
        return param
    if param.endswith("_") and param[:-1] in json_keys:
        return param[:-1]
    return param


def class_name(raw: str, suffix: str = "") -> str:
    """``list_transactions`` -> ``ListTransactions``."""
    parts = [p for p in _NON_IDENT.sub("_", raw).split("_") if p]
    name = "".join(p[:1].upper() + p[1:] for p in parts)
    if not name or name[0].isdigit():
        name = f"T{name}"
    return f"{name}{suffix}"
