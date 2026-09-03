"""Key translation between the wire and the stubs.

The contract these tests defend is a round trip: for any schema, whatever the
stub generator spells with a trailing underscore is what `to_python_keys`
produces and what `to_json_keys` undoes. The last test in this file checks that
property against the generator itself, so the two cannot drift apart silently.
"""

from __future__ import annotations

from typing import Any

import pytest

from runlace.jsonschema_py import TypeRenderer, defs_of
from runlace.keys import to_json_keys, to_python_keys

RELATION = {
    "type": "object",
    "properties": {
        "from": {"type": "string"},
        "to": {"type": "string"},
        "relationType": {"type": "string"},
    },
    "required": ["from", "to", "relationType"],
}

# @modelcontextprotocol/server-memory, verbatim. The schema that found the bug.
CREATE_RELATIONS = {
    "type": "object",
    "properties": {"relations": {"type": "array", "items": RELATION}},
    "required": ["relations"],
}


# -- the reported bug ------------------------------------------------------


def test_a_reserved_key_inside_an_array_of_objects_is_mapped() -> None:
    arguments = {"relations": [{"from_": "Alice", "to": "Runlace", "relationType": "maintains"}]}
    assert to_json_keys(arguments, CREATE_RELATIONS) == {
        "relations": [{"from": "Alice", "to": "Runlace", "relationType": "maintains"}]
    }


def test_a_reserved_key_inside_a_result_is_mapped_back() -> None:
    schema = {
        "type": "object",
        "properties": {"relations": {"type": "array", "items": RELATION}},
    }
    result = {"relations": [{"from": "Alice", "to": "Runlace", "relationType": "maintains"}]}
    assert to_python_keys(result, schema) == {
        "relations": [{"from_": "Alice", "to": "Runlace", "relationType": "maintains"}]
    }


def test_the_two_directions_undo_each_other() -> None:
    wire = {"relations": [{"from": "a", "to": "b", "relationType": "c"}]}
    assert to_json_keys(to_python_keys(wire, CREATE_RELATIONS), CREATE_RELATIONS) == wire


# -- the shapes a schema can take ------------------------------------------


def test_top_level_arguments_are_still_mapped() -> None:
    schema = {"type": "object", "properties": {"from": {"type": "string"}}}
    assert to_json_keys({"from_": "x"}, schema) == {"from": "x"}


def test_a_key_that_is_not_reserved_is_left_exactly_as_it_is() -> None:
    schema = {"type": "object", "properties": {"entityName": {"type": "string"}}}
    arguments = {"entityName": "Alice"}
    assert to_json_keys(arguments, schema) == arguments
    assert to_python_keys(arguments, schema) == arguments


def test_nesting_goes_all_the_way_down() -> None:
    schema = {
        "type": "object",
        "properties": {
            "outer": {
                "type": "object",
                "properties": {
                    "rows": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"class": {"type": "integer"}},
                        },
                    }
                },
            }
        },
    }
    assert to_json_keys({"outer": {"rows": [{"class_": 1}, {"class_": 2}]}}, schema) == {
        "outer": {"rows": [{"class": 1}, {"class": 2}]}
    }


def test_a_ref_is_followed() -> None:
    schema = {
        "type": "object",
        "$defs": {"Relation": RELATION},
        "properties": {"relation": {"$ref": "#/$defs/Relation"}},
    }
    assert to_json_keys({"relation": {"from_": "a"}}, schema) == {"relation": {"from": "a"}}


def test_a_nullable_ref_is_followed_through_the_union() -> None:
    """`anyOf: [ref, null]` is how optional objects are usually spelled."""
    schema = {
        "type": "object",
        "$defs": {"Relation": RELATION},
        "properties": {
            "relation": {"anyOf": [{"$ref": "#/$defs/Relation"}, {"type": "null"}]}
        },
    }
    assert to_json_keys({"relation": {"from_": "a"}}, schema) == {"relation": {"from": "a"}}
    assert to_json_keys({"relation": None}, schema) == {"relation": None}


def test_a_single_allof_is_followed() -> None:
    schema = {"type": "object", "properties": {"relation": {"allOf": [RELATION]}}}
    assert to_json_keys({"relation": {"from_": "a"}}, schema) == {"relation": {"from": "a"}}


def test_a_type_list_is_followed() -> None:
    schema = {
        "type": "object",
        "properties": {
            "relation": {"type": ["object", "null"], "properties": RELATION["properties"]}
        },
    }
    assert to_json_keys({"relation": {"from_": "a"}}, schema) == {"relation": {"from": "a"}}


def test_free_form_object_values_are_still_walked() -> None:
    """`additionalProperties` renders as dict[str, V]: the keys are the caller's."""
    schema = {
        "type": "object",
        "properties": {"byName": {"type": "object", "additionalProperties": RELATION}},
    }
    assert to_json_keys({"byName": {"any key": {"from_": "a"}}}, schema) == {
        "byName": {"any key": {"from": "a"}}
    }


# -- where the renderer gives up, so do we ---------------------------------


def test_an_object_with_an_unspellable_key_is_left_alone() -> None:
    """The renderer emits dict[str, object] here, so no key was ever renamed."""
    schema = {
        "type": "object",
        "properties": {
            "headers": {
                "type": "object",
                "properties": {"content-type": {"type": "string"}, "from": {"type": "string"}},
            }
        },
    }
    value = {"headers": {"content-type": "text/plain", "from": "a"}}
    assert to_json_keys(value, schema) == value
    assert to_python_keys(value, schema) == value


def test_an_enum_is_a_literal_and_has_no_keys_to_rename() -> None:
    schema = {"type": "object", "properties": {"mode": {"enum": ["a", "b"]}}}
    assert to_json_keys({"mode": "a"}, schema) == {"mode": "a"}


def test_a_tuple_style_array_is_left_alone() -> None:
    """`items` as a list renders as list[object]: not worth modelling, D2 or not."""
    schema = {"type": "object", "properties": {"pair": {"type": "array", "items": [RELATION]}}}
    value = {"pair": [{"from_": "a"}]}
    assert to_json_keys(value, schema) == value


def test_a_missing_schema_changes_nothing() -> None:
    value = {"from_": "a", "nested": [{"from_": "b"}]}
    assert to_json_keys(value, None) == value
    assert to_python_keys(value, None) == value


def test_a_key_the_schema_never_mentions_is_passed_through() -> None:
    schema = {"type": "object", "properties": {"to": {"type": "string"}}}
    assert to_json_keys({"to": "a", "surprise": 1}, schema) == {"to": "a", "surprise": 1}


def test_a_value_of_the_wrong_shape_is_passed_through_untouched() -> None:
    """Validation is D7's job and happens elsewhere; this only renames."""
    assert to_json_keys("not an object", CREATE_RELATIONS) == "not an object"
    assert to_json_keys({"relations": "not a list"}, CREATE_RELATIONS) == {
        "relations": "not a list"
    }


def test_the_caller_s_value_is_not_mutated() -> None:
    arguments = {"relations": [{"from_": "a"}]}
    to_json_keys(arguments, CREATE_RELATIONS)
    assert arguments == {"relations": [{"from_": "a"}]}


def test_a_self_referential_schema_terminates() -> None:
    schema: dict[str, Any] = {
        "type": "object",
        "$defs": {
            "Node": {
                "type": "object",
                "properties": {
                    "from": {"type": "string"},
                    "child": {"$ref": "#/$defs/Node"},
                },
            }
        },
        "properties": {"root": {"$ref": "#/$defs/Node"}},
    }
    value: dict[str, Any] = {"from_": "leaf"}
    for _ in range(4):
        value = {"from_": "x", "child": value}

    mapped = to_json_keys({"root": value}, schema)
    assert mapped == {
        "root": {
            "from": "x",
            "child": {
                "from": "x",
                "child": {"from": "x", "child": {"from": "x", "child": {"from": "leaf"}}}
            },
        }
    }


# -- the invariant that keeps this in step with the stubs ------------------


def sample(schema: Any, defs: dict[str, Any] | None = None) -> Any:
    """A value with every property the schema declares, filled with placeholders."""
    if not isinstance(schema, dict):
        return "x"
    defs = defs_of(schema, defs or {})
    ref = schema.get("$ref")
    if isinstance(ref, str):
        return sample(defs.get(ref.rsplit("/", 1)[-1]), defs)
    if schema.get("type") == "array":
        return [sample(schema.get("items"), defs)]
    properties = schema.get("properties")
    if isinstance(properties, dict) and properties:
        return {str(key): sample(sub, defs) for key, sub in properties.items()}
    return "x"


def every_key(value: Any) -> set[str]:
    if isinstance(value, dict):
        keys: set[str] = set()
        for key, item in value.items():  # pyright: ignore[reportUnknownVariableType]
            keys.add(str(key))
            keys |= every_key(item)
        return keys
    if isinstance(value, list):
        return set().union(*(every_key(item) for item in value)) if value else set()  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]
    return set()


def declared_names(schema: Any) -> set[str]:
    """Every field name the stub generator writes for this schema."""
    renderer = TypeRenderer()
    renderer.render(schema, "Probe")
    names: set[str] = set()
    for block in renderer.blocks:
        for line in block.splitlines()[1:]:
            names.add(line.strip().split(":", 1)[0])
    return names


@pytest.mark.parametrize(
    "schema",
    [
        RELATION,
        CREATE_RELATIONS,
        {"type": "object", "properties": {"class": {"type": "integer"}}},
        {"type": "object", "properties": {"lambda": {"type": "array", "items": RELATION}}},
        {
            "type": "object",
            "$defs": {"Relation": RELATION},
            "properties": {"rel": {"$ref": "#/$defs/Relation"}, "if": {"type": "string"}},
        },
    ],
    ids=["relation", "create_relations", "class", "lambda", "ref"],
)
def test_a_workflow_sees_exactly_the_keys_its_stub_declares(schema: dict[str, Any]) -> None:
    """The generator is the source of truth for the spelling; this proves we follow it.

    Every field name in the stub must turn up in what the workflow is handed,
    and nothing else must -- otherwise a typechecked subscript raises KeyError.
    """
    wire = sample(schema)
    assert every_key(to_python_keys(wire, schema)) == declared_names(schema)
    assert to_json_keys(to_python_keys(wire, schema), schema) == wire
