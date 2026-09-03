"""Standing in for a tool a dry run refuses to call.

The value never has to be plausible. It has to be the right shape, because the
line after the call is what a dry run exists to exercise -- a subscript, a
`len`, a comparison. Every test here is really the same question: would the
next line of a workflow survive this?
"""

from __future__ import annotations

from typing import Any

import pytest

from runlace.simulate import LIST_LENGTH, stand_in


def test_a_tool_with_no_output_schema_stands_in_as_none() -> None:
    """D2 types those as `Any`, and inventing a shape nobody promised is worse.

    The workflow will probably fail on the next line, and that failure is real
    information: the code assumed something the server never said.
    """
    assert stand_in(None) is None


def test_an_object_comes_back_with_every_declared_property() -> None:
    value = stand_in(
        {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "count": {"type": "integer"},
                "ok": {"type": "boolean"},
            },
            "required": ["id"],
        }
    )
    # Not only `required`: an optional key the real server does send would
    # otherwise fail the dry run on a line that was fine.
    assert value == {"id": "", "count": 1, "ok": False}


def test_a_list_has_exactly_one_element_so_a_loop_body_runs_once() -> None:
    value = stand_in(
        {
            "type": "object",
            "properties": {
                "transactions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"amount": {"type": "number"}},
                    },
                }
            },
        }
    )
    assert value == {"transactions": [{"amount": 1}]}
    assert len(value["transactions"]) == LIST_LENGTH


def test_a_number_is_never_zero() -> None:
    """A stand-in in a denominator must not invent a ZeroDivisionError."""
    assert stand_in({"type": "number"}) != 0


def test_the_server_s_own_default_wins_over_anything_we_would_invent() -> None:
    assert stand_in({"type": "string", "default": "ops@example.com"}) == "ops@example.com"


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        ({"const": "fixed"}, "fixed"),
        ({"enum": ["sent", "queued"]}, "sent"),
        ({"anyOf": [{"type": "integer"}, {"type": "string"}]}, 1),
        ({"oneOf": [{"type": "boolean"}]}, False),
    ],
)
def test_the_narrower_keyword_is_taken_before_the_type(
    schema: dict[str, Any], expected: Any
) -> None:
    assert stand_in(schema) == expected


def test_a_nullable_type_takes_the_branch_that_is_not_null() -> None:
    """`["string", "null"]` means "usually a string". None proves nothing."""
    assert stand_in({"type": ["null", "string"]}) == ""


def test_a_ref_into_defs_is_resolved() -> None:
    value = stand_in(
        {
            "$defs": {"Item": {"type": "object", "properties": {"id": {"type": "string"}}}},
            "type": "object",
            "properties": {"item": {"$ref": "#/$defs/Item"}},
        }
    )
    assert value == {"item": {"id": ""}}


def test_a_ref_that_leads_nowhere_does_not_hang() -> None:
    """A `$ref` cycle is a server's bug, not a reason to lose the dry run."""
    value = stand_in(
        {
            "$defs": {"Node": {"$ref": "#/$defs/Node"}},
            "$ref": "#/$defs/Node",
        }
    )
    assert value is None


def test_a_schema_with_properties_but_no_type_is_still_an_object() -> None:
    """Servers leave `type` off constantly, and an object is what they mean."""
    assert stand_in({"properties": {"id": {"type": "string"}}}) == {"id": ""}
