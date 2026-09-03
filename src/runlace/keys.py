"""Translating keys between the wire and the stubs.

:mod:`runlace.jsonschema_py` decides how a JSON Schema is spelled in Python, and
one of its decisions is that a reserved property name grows a trailing
underscore: ``from`` becomes ``from_``. It does that at *every* level of a
schema, not just for a tool's keyword arguments, so the runtime has to undo it
at every level too. A workflow writes

    ctx.memory.create_relations(relations=[{"from_": "Alice", "to": "Runlace"}])

because that is what the stub declares, and the server expects ``from`` inside
each item of that list. The same applies coming back: the stub says a relation
has ``from_``, so the result has to be handed to the workflow with that key.

These two functions walk a value alongside the schema that describes it and
rename exactly the keys the stub generator renamed -- no more. They mirror
:meth:`TypeRenderer._render`, and they have to keep mirroring it: wherever the
renderer gives up and emits ``object`` or ``dict[str, object]``, the stub
promised no spelling at all, so nothing here may be renamed either.
"""

from __future__ import annotations

from typing import Any

from .jsonschema_py import MAX_DEPTH, defs_of
from .naming import param_name, unmap_param_name


def to_json_keys(value: Any, schema: Any) -> Any:
    """Python spelling -> JSON spelling, for arguments on their way to a server."""
    return _walk(value, schema, {}, 0, to_python=False)


def to_python_keys(value: Any, schema: Any) -> Any:
    """JSON spelling -> Python spelling, for a result on its way to a workflow."""
    return _walk(value, schema, {}, 0, to_python=True)


def _walk(value: Any, schema: Any, defs: dict[str, Any], depth: int, *, to_python: bool) -> Any:
    if depth > MAX_DEPTH or not isinstance(schema, dict) or not schema:
        return value

    defs = defs_of(schema, defs)

    ref = schema.get("$ref")
    if isinstance(ref, str):
        target = defs.get(ref.rsplit("/", 1)[-1])
        return _walk(value, target, defs, depth + 1, to_python=to_python)

    # const and enum render as Literals, and a Literal has no keys to rename.
    if "const" in schema or isinstance(schema.get("enum"), list) and schema.get("enum"):
        return value

    for combinator in ("anyOf", "oneOf"):
        members = schema.get(combinator)
        if isinstance(members, list) and members:
            return _apply_each(value, members, defs, depth, to_python=to_python)

    all_of = schema.get("allOf")
    if isinstance(all_of, list) and len(all_of) == 1:
        return _walk(value, all_of[0], defs, depth + 1, to_python=to_python)

    type_ = schema.get("type")
    if isinstance(type_, list):
        branches = [{**schema, "type": t} for t in type_]
        return _apply_each(value, branches, defs, depth, to_python=to_python)

    if type_ == "array":
        items = schema.get("items")
        if not isinstance(value, list) or isinstance(items, list):
            return value
        return [_walk(item, items, defs, depth + 1, to_python=to_python) for item in value]

    if type_ == "object" or (type_ is None and "properties" in schema):
        return _walk_object(value, schema, defs, depth, to_python=to_python)

    return value


def _apply_each(
    value: Any, members: list[Any], defs: dict[str, Any], depth: int, *, to_python: bool
) -> Any:
    """Run the value through every branch of a union, in order.

    A branch that does not describe this value leaves it alone -- ``{"type":
    "null"}`` next to a ``$ref`` is the common shape -- and a branch that has
    already been applied is a no-op the second time, because both directions
    only rename a key when the *other* spelling is the one in the schema. So
    chaining is safe, and it saves us guessing which branch the value matches.
    """
    for member in members:
        value = _walk(value, member, defs, depth + 1, to_python=to_python)
    return value


def _walk_object(
    value: Any, schema: dict[str, Any], defs: dict[str, Any], depth: int, *, to_python: bool
) -> Any:
    if not isinstance(value, dict):
        return value
    members: dict[str, Any] = {str(key): item for key, item in value.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]

    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        additional = schema.get("additionalProperties")
        if isinstance(additional, dict):
            # dict[str, V]: the keys are the caller's, only the values have a shape.
            return {
                key: _walk(item, additional, defs, depth + 1, to_python=to_python)
                for key, item in members.items()
            }
        return value

    json_keys = [str(key) for key in properties]
    py_names = {key: param_name(key) for key in json_keys}
    if any(name is None for name in py_names.values()):
        # One unspellable key and the renderer emits `dict[str, object]` for the
        # whole shape, without descending. Nothing here was renamed, so nothing
        # here gets unrenamed.
        return value

    renamed: dict[str, Any] = {}
    for key, item in members.items():
        json_key = key if to_python else unmap_param_name(key, json_keys)
        new_key = (py_names.get(key) or key) if to_python else json_key
        renamed[new_key] = _walk(
            item, properties.get(json_key), defs, depth + 1, to_python=to_python
        )
    return renamed
