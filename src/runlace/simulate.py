"""Stand-in values for the tools a dry run refuses to actually call.

A dry run executes the workflow for real. Every read happens against the live
server, every branch is taken, the return value is validated. The one thing it
will not do is let a side-effecting tool act, so those calls are answered from
the tool's own ``outputSchema`` instead of going over the wire.

The values are deliberately dull. A stand-in exists so the line after the call
has something of the right shape to work with -- not to be realistic. Code that
branches on what a side effect returned is the case a dry run cannot check, and
that is stated in the result rather than papered over.
"""

from __future__ import annotations

from typing import Any

# One element, so a `for` over a stand-in list runs its body exactly once. Zero
# would skip the loop and prove nothing; more would only repeat it.
LIST_LENGTH = 1

# Deep enough for any real tool schema, shallow enough that a `$ref` cycle we
# failed to resolve cannot hang the run.
MAX_DEPTH = 8

PLACEHOLDER_STRING = ""

# Not 0: a stand-in that lands in a denominator would fail the dry run with a
# ZeroDivisionError the real call would never have caused.
PLACEHOLDER_NUMBER = 1


def stand_in(schema: dict[str, Any] | None) -> Any:
    """A value of the shape ``schema`` describes, or ``None`` if it describes none.

    ``None`` is the honest answer for a tool with no ``outputSchema`` (D2 types
    those as ``Any``). It will usually make the workflow fail, and that failure
    is real information: the code assumed a shape nobody promised.
    """
    if schema is None:
        return None
    return _value(schema, schema.get("$defs") or {}, 0)


def _value(schema: Any, defs: dict[str, Any], depth: int) -> Any:
    if not isinstance(schema, dict) or depth > MAX_DEPTH:
        return None

    resolved = _resolve(schema, defs)
    if resolved is not schema:
        return _value(resolved, defs, depth + 1)

    if "default" in schema:
        # The server said what it usually sends. Nothing we invent beats that.
        return schema["default"]
    if "const" in schema:
        return schema["const"]
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]

    for keyword in ("anyOf", "oneOf", "allOf"):
        members = schema.get(keyword)
        if isinstance(members, list) and members:
            return _value(members[0], defs, depth + 1)

    return _by_type(_type_of(schema), schema, defs, depth)


def _by_type(kind: str | None, schema: dict[str, Any], defs: dict[str, Any], depth: int) -> Any:
    if kind == "object":
        return _object(schema, defs, depth)
    if kind == "array":
        return [_value(schema.get("items"), defs, depth + 1) for _ in range(LIST_LENGTH)]
    if kind == "string":
        return PLACEHOLDER_STRING
    if kind in ("integer", "number"):
        return PLACEHOLDER_NUMBER
    if kind == "boolean":
        return False
    if kind == "null":
        return None
    # No `type` at all, which servers leave off constantly. If it declares
    # properties it meant an object, so fill them; otherwise there is nothing to
    # go on and None is the honest answer.
    return _object(schema, defs, depth) if "properties" in schema else None


def _object(schema: dict[str, Any], defs: dict[str, Any], depth: int) -> dict[str, Any]:
    """Every declared property, not only the required ones.

    Filling only `required` would be the stricter reading, but it would make the
    dry run fail on optional keys the real server does send -- a false alarm
    about a line that was fine. Missing keys are what validation is for.
    """
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return {}
    return {
        str(name): _value(subschema, _merged(schema, defs), depth + 1)
        for name, subschema in properties.items()
    }


def _type_of(schema: dict[str, Any]) -> str | None:
    kind = schema.get("type")
    if isinstance(kind, str):
        return kind
    if isinstance(kind, list):
        # A union of types. `null` is the one nobody means as the interesting
        # branch, so it is taken last.
        named = [k for k in kind if isinstance(k, str)]
        return next((k for k in named if k != "null"), named[0] if named else None)
    return None


def _resolve(schema: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref")
    if not isinstance(ref, str):
        return schema
    prefix = "#/$defs/"
    if not ref.startswith(prefix):
        return schema
    target = defs.get(ref[len(prefix) :])
    return target if isinstance(target, dict) else schema


def _merged(schema: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    local = schema.get("$defs")
    return {**defs, **local} if isinstance(local, dict) else defs
