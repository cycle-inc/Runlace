"""Validating a run's inputs and its output against the declared schemas (D7).

pyright already checked the workflow's *code* against these schemas at create
time. This is the other half: the values a caller actually passes, and the value
the workflow actually returns, checked with Pydantic so a failure comes back as
per-field errors an agent can act on.

A JSON Schema is turned into a Python annotation and handed to a
``TypeAdapter``. Object schemas become ``TypedDict``s, so Pydantic reports the
JSON key verbatim -- ``ctx.inputs["from"]`` is reported as ``from``, not as some
Python spelling of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Union

from pydantic import TypeAdapter, ValidationError
from typing_extensions import NotRequired, TypedDict

from .naming import class_name

# Same guard as stub generation: a schema that references itself through $ref
# would otherwise recurse forever.
_MAX_DEPTH = 12

_PRIMITIVES: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "null": type(None),
}


@dataclass(frozen=True)
class FieldError:
    """One thing wrong with a value, addressed to whoever supplied it."""

    field: str
    message: str

    def to_json(self) -> dict[str, Any]:
        return {"field": self.field, "message": self.message}

    def __str__(self) -> str:
        return f"{self.field}: {self.message}" if self.field else self.message


def validate(schema: dict[str, Any] | None, value: Any) -> list[FieldError]:
    """Check ``value`` against ``schema``. An empty list means it is fine.

    No schema means no constraint: workflows may declare neither an
    ``inputs_schema`` nor an ``outputs_schema``, and an absent contract cannot
    be violated.
    """
    if not schema:
        return []
    try:
        adapter = TypeAdapter(annotation_for(schema))
    except Exception:  # noqa: BLE001 - a schema we cannot model must not block a run
        return []
    try:
        adapter.validate_python(value)
    except ValidationError as exc:
        return [
            FieldError(field=_path(error.get("loc", ())), message=str(error.get("msg")))
            for error in exc.errors(include_url=False)
        ]
    return []


def apply_defaults(schema: dict[str, Any] | None, inputs: dict[str, Any]) -> dict[str, Any]:
    """Fill in top-level ``default`` values for keys the caller left out.

    Only the top level, and only keys that are absent: a declared default that
    did nothing would be a trap for the agent that declared it, and reaching
    into nested objects to invent values would be guesswork.
    """
    if not schema:
        return inputs
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return inputs
    filled = dict(inputs)
    for key, subschema in properties.items():
        if isinstance(subschema, dict) and "default" in subschema and key not in filled:
            filled[str(key)] = subschema["default"]
    return filled


def _path(loc: Any) -> str:
    """Pydantic's error location as a JSON-ish path: ``recipients.0.email``."""
    if not isinstance(loc, tuple):
        return ""
    return ".".join(str(part) for part in loc)


# -- JSON Schema to Python annotation --------------------------------------


def annotation_for(schema: Any, name: str = "Value") -> Any:
    return _annotation(schema, name, {}, depth=0)


def _annotation(schema: Any, hint: str, defs: dict[str, Any], depth: int) -> Any:
    if depth > _MAX_DEPTH or not isinstance(schema, dict) or not schema:
        return Any

    defs = _merge_defs(schema, defs)

    ref = schema.get("$ref")
    if isinstance(ref, str):
        target = defs.get(ref.rsplit("/", 1)[-1])
        return _annotation(target, hint, defs, depth + 1) if target is not None else Any

    if "const" in schema:
        return _literal([schema["const"]])

    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return _literal(enum)

    for combinator in ("anyOf", "oneOf"):
        members = schema.get(combinator)
        if isinstance(members, list) and members:
            return _union([_annotation(m, hint, defs, depth + 1) for m in members])

    all_of = schema.get("allOf")
    if isinstance(all_of, list) and len(all_of) == 1:
        return _annotation(all_of[0], hint, defs, depth + 1)

    type_ = schema.get("type")
    if isinstance(type_, list):
        return _union(
            [_annotation({**schema, "type": t}, hint, defs, depth + 1) for t in type_]
        )

    if type_ == "array":
        items = schema.get("items")
        if isinstance(items, list):
            return list[Any]
        return list[_annotation(items, f"{hint}Item", defs, depth + 1)]  # type: ignore[misc]

    if type_ == "object" or (type_ is None and "properties" in schema):
        return _object(schema, hint, defs, depth)

    if isinstance(type_, str) and type_ in _PRIMITIVES:
        return _PRIMITIVES[type_]

    return Any


def _object(schema: dict[str, Any], hint: str, defs: dict[str, Any], depth: int) -> Any:
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        additional = schema.get("additionalProperties")
        if isinstance(additional, dict):
            return dict[str, _annotation(additional, f"{hint}Value", defs, depth + 1)]  # type: ignore[misc]
        return dict[str, Any]

    required = schema.get("required")
    required_keys = set(required) if isinstance(required, list) else set()

    entries: dict[str, Any] = {}
    for key, subschema in properties.items():
        annotation = _annotation(
            subschema, f"{hint}{class_name(str(key))}", defs, depth + 1
        )
        if key not in required_keys:
            annotation = NotRequired[annotation]
        entries[str(key)] = annotation

    # The functional form takes the JSON keys verbatim, reserved words and
    # hyphens included, which is what makes the error paths readable.
    return TypedDict(class_name(hint), entries)  # type: ignore[operator]


def _literal(values: list[Any]) -> Any:
    members = [v for v in values if isinstance(v, (str, int, bool))]
    if len(members) != len(values) or not members:
        return Any
    return Literal[tuple(members)]  # type: ignore[return-value]


def _union(members: list[Any]) -> Any:
    if Any in members:
        return Any
    unique: list[Any] = []
    for member in members:
        if member not in unique:
            unique.append(member)
    if not unique:
        return Any
    if len(unique) == 1:
        return unique[0]
    return Union[tuple(unique)]


def _merge_defs(schema: dict[str, Any], inherited: dict[str, Any]) -> dict[str, Any]:
    merged = dict(inherited)
    for key in ("$defs", "definitions"):
        local = schema.get(key)
        if isinstance(local, dict):
            merged.update(local)
    return merged
