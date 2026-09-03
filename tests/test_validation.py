"""Runtime schema validation (D7).

What matters here is not that bad values are rejected -- Pydantic does that --
but that the rejection names the field the way the JSON does, so an agent can
map an error back to a key it passed.
"""

from __future__ import annotations

from typing import Any

from runlace.validation import FieldError, apply_defaults, validate

PERIOD = {
    "type": "object",
    "properties": {
        "from": {"type": "string"},
        "to": {"type": "string"},
        "limit": {"type": "integer", "default": 50},
    },
    "required": ["from", "to"],
}


def fields(errors: list[FieldError]) -> list[str]:
    return [e.field for e in errors]


def test_a_valid_value_produces_no_errors() -> None:
    assert validate(PERIOD, {"from": "2024-01-01", "to": "2024-12-31"}) == []


def test_a_missing_required_key_is_reported_by_name() -> None:
    errors = validate(PERIOD, {"from": "2024-01-01"})
    assert fields(errors) == ["to"]
    assert "required" in errors[0].message.lower()


def test_a_reserved_word_key_is_reported_verbatim() -> None:
    """`from` is a Python keyword; the error must still call it `from`."""
    errors = validate(PERIOD, {"from": 7, "to": "2024-12-31"})
    assert fields(errors) == ["from"]
    assert str(errors[0]).startswith("from: ")


def test_a_hyphenated_key_survives_too() -> None:
    schema = {
        "type": "object",
        "properties": {"content-type": {"type": "string"}},
        "required": ["content-type"],
    }
    assert fields(validate(schema, {})) == ["content-type"]


def test_nested_errors_carry_a_path() -> None:
    schema = {
        "type": "object",
        "properties": {
            "recipients": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"email": {"type": "string"}},
                    "required": ["email"],
                },
            }
        },
        "required": ["recipients"],
    }
    errors = validate(schema, {"recipients": [{"email": "a@b.c"}, {}]})
    assert fields(errors) == ["recipients.1.email"]


def test_every_bad_field_is_reported_at_once() -> None:
    """An agent should be able to fix one call, not discover errors one at a time."""
    errors = validate(PERIOD, {"from": 1, "to": 2})
    assert sorted(fields(errors)) == ["from", "to"]


def test_extra_keys_are_allowed() -> None:
    """JSON Schema objects are open by default, and so is Runlace."""
    assert validate(PERIOD, {"from": "a", "to": "b", "note": "hi"}) == []


def test_optional_keys_may_be_absent() -> None:
    assert validate(PERIOD, {"from": "a", "to": "b"}) == []


def test_enums_and_unions_are_enforced() -> None:
    schema = {
        "type": "object",
        "properties": {"mode": {"enum": ["fast", "slow"]}},
        "required": ["mode"],
    }
    assert validate(schema, {"mode": "fast"}) == []
    assert fields(validate(schema, {"mode": "sideways"})) == ["mode"]


def test_a_nullable_type_list_accepts_both() -> None:
    schema = {
        "type": "object",
        "properties": {"note": {"type": ["string", "null"]}},
        "required": ["note"],
    }
    assert validate(schema, {"note": None}) == []
    assert validate(schema, {"note": "hi"}) == []
    assert fields(validate(schema, {"note": 3})) == ["note"]


def test_a_ref_is_followed() -> None:
    schema = {
        "$defs": {"Money": {"type": "number"}},
        "type": "object",
        "properties": {"total": {"$ref": "#/$defs/Money"}},
        "required": ["total"],
    }
    assert validate(schema, {"total": 4.5}) == []
    assert fields(validate(schema, {"total": "lots"})) == ["total"]


def test_no_schema_means_no_constraint() -> None:
    """Workflows may declare neither schema; an absent contract cannot be broken."""
    assert validate(None, {"anything": 1}) == []
    assert validate({}, "not even an object") == []


def test_a_schema_that_cannot_be_modelled_does_not_block_a_run() -> None:
    """Better to run than to refuse a workflow over a schema Runlace misread."""
    weird: dict[str, Any] = {"type": "object", "properties": {"x": {"$ref": "#/nope"}}}
    assert validate(weird, {"x": object()}) == []


def test_a_non_object_output_schema_is_checked_too() -> None:
    assert validate({"type": "array", "items": {"type": "integer"}}, [1, 2]) == []
    assert validate({"type": "array", "items": {"type": "integer"}}, ["a"]) != []


# -- defaults --------------------------------------------------------------


def test_defaults_fill_absent_keys() -> None:
    assert apply_defaults(PERIOD, {"from": "a", "to": "b"})["limit"] == 50


def test_defaults_never_overwrite_what_the_caller_passed() -> None:
    given = {"from": "a", "to": "b", "limit": 5}
    assert apply_defaults(PERIOD, given)["limit"] == 5


def test_defaults_do_not_mutate_the_callers_dict() -> None:
    given: dict[str, Any] = {"from": "a", "to": "b"}
    apply_defaults(PERIOD, given)
    assert given == {"from": "a", "to": "b"}


def test_defaults_are_top_level_only() -> None:
    """Inventing values inside nested objects would be guesswork."""
    schema = {
        "type": "object",
        "properties": {
            "page": {
                "type": "object",
                "properties": {"size": {"type": "integer", "default": 10}},
            }
        },
    }
    assert apply_defaults(schema, {}) == {}
