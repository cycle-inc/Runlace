from __future__ import annotations

from runlace.hashing import canonical_json, schema_hash

INPUT = {"type": "object", "properties": {"a": {"type": "string"}}}


def test_canonical_json_is_key_order_independent() -> None:
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


def test_same_schema_hashes_the_same() -> None:
    assert schema_hash(INPUT, None) == schema_hash(dict(INPUT), None)


def test_changed_parameter_changes_the_hash() -> None:
    changed = {"type": "object", "properties": {"a": {"type": "integer"}}}
    assert schema_hash(INPUT, None) != schema_hash(changed, None)


def test_added_parameter_changes_the_hash() -> None:
    added = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}
    assert schema_hash(INPUT, None) != schema_hash(added, None)


def test_output_schema_is_part_of_the_hash() -> None:
    assert schema_hash(INPUT, None) != schema_hash(INPUT, {"type": "object"})


def test_missing_and_empty_input_schema_differ() -> None:
    assert schema_hash(None, None) != schema_hash({}, None)
