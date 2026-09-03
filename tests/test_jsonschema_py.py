from __future__ import annotations

from typing import Any

from runlace.jsonschema_py import TypeRenderer


def render(schema: Any, hint: str = "T") -> tuple[str, TypeRenderer]:
    renderer = TypeRenderer()
    return renderer.render(schema, hint), renderer


def test_primitives() -> None:
    assert render({"type": "string"})[0] == "str"
    assert render({"type": "integer"})[0] == "int"
    assert render({"type": "number"})[0] == "float"
    assert render({"type": "boolean"})[0] == "bool"


def test_arrays() -> None:
    assert render({"type": "array", "items": {"type": "string"}})[0] == "list[str]"
    assert render({"type": "array"})[0] == "list[object]"


def test_nullable_type_list() -> None:
    assert render({"type": ["string", "null"]})[0] == "str | None"


def test_string_enum_becomes_a_literal() -> None:
    expr, renderer = render({"type": "string", "enum": ["a", "b"]})
    assert expr == "Literal['a', 'b']"
    assert "Literal" in renderer.typing_imports


def test_mixed_enum_falls_back_to_object() -> None:
    assert render({"enum": ["a", {"nested": 1}]})[0] == "object"


def test_unknown_schema_is_object() -> None:
    assert render({})[0] == "object"
    assert render(None)[0] == "object"
    assert render({"type": "weird"})[0] == "object"


def test_object_without_properties_is_a_plain_mapping() -> None:
    assert render({"type": "object"})[0] == "dict[str, object]"
    assert (
        render({"type": "object", "additionalProperties": {"type": "integer"}})[0]
        == "dict[str, int]"
    )


def test_object_with_properties_becomes_a_typed_dict() -> None:
    expr, renderer = render(
        {
            "type": "object",
            "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
            "required": ["a"],
        },
        hint="Result",
    )
    assert expr == "Result"
    block = renderer.blocks[0]
    assert "class Result(TypedDict):" in block
    assert "a: str" in block
    assert "b: NotRequired[int]" in block


def test_nested_objects_are_defined_before_their_parent() -> None:
    _, renderer = render(
        {
            "type": "object",
            "properties": {"inner": {"type": "object", "properties": {"x": {"type": "string"}}}},
        },
        hint="Outer",
    )
    assert len(renderer.blocks) == 2
    assert "class OuterInner(TypedDict):" in renderer.blocks[0]
    assert "inner: NotRequired[OuterInner]" in renderer.blocks[1]


def test_union_containing_an_unknown_collapses_to_object() -> None:
    assert render({"anyOf": [{"type": "string"}, {}]})[0] == "object"


def test_union_of_known_types() -> None:
    assert render({"anyOf": [{"type": "string"}, {"type": "integer"}]})[0] == "str | int"


def test_ref_is_resolved_against_defs() -> None:
    expr, renderer = render(
        {
            "$defs": {"Node": {"type": "object", "properties": {"id": {"type": "string"}}}},
            "$ref": "#/$defs/Node",
        }
    )
    assert expr == "Node"
    assert "class Node(TypedDict):" in renderer.blocks[0]


def test_self_referencing_ref_terminates() -> None:
    expr, _ = render(
        {
            "$defs": {
                "Node": {
                    "type": "object",
                    "properties": {"children": {"type": "array", "items": {"$ref": "#/$defs/Node"}}},
                }
            },
            "$ref": "#/$defs/Node",
        }
    )
    assert expr == "Node"


def test_unresolvable_ref_is_object() -> None:
    assert render({"$ref": "#/$defs/Missing"})[0] == "object"


def test_object_fields_orders_required_first_and_renames_keywords() -> None:
    renderer = TypeRenderer()
    shape = renderer.object_fields(
        {
            "type": "object",
            "properties": {"to": {"type": "string"}, "from": {"type": "string"}},
            "required": ["from"],
        },
        hint="Send",
    )
    assert [(f.py_name, f.required) for f in shape.fields] == [("from_", True), ("to", False)]
    assert shape.fields[0].json_key == "from"
    assert shape.fields[0].renamed is True


def test_object_fields_reports_a_key_that_cannot_be_a_kwarg() -> None:
    renderer = TypeRenderer()
    shape = renderer.object_fields(
        {"type": "object", "properties": {"content-type": {"type": "string"}}}, hint="X"
    )
    assert shape.unmappable == "content-type"
    assert shape.fields == []


def test_nested_object_with_an_unspellable_key_degrades_to_a_mapping() -> None:
    assert (
        render({"type": "object", "properties": {"content-type": {"type": "string"}}})[0]
        == "dict[str, object]"
    )


def test_typed_dict_names_are_deduplicated() -> None:
    renderer = TypeRenderer()
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    assert renderer.render(schema, "Dup") == "Dup"
    assert renderer.render(schema, "Dup") == "Dup2"
