from __future__ import annotations

from runlace.risk import READ_ONLY, SIDE_EFFECT, classify


def test_read_only_hint_true_is_read_only() -> None:
    assert classify({"readOnlyHint": True}) == READ_ONLY


def test_read_only_hint_false_is_side_effect() -> None:
    assert classify({"readOnlyHint": False}) == SIDE_EFFECT


def test_unannotated_defaults_to_side_effect() -> None:
    # D5: absent annotations must never be read as "safe".
    assert classify(None) == SIDE_EFFECT
    assert classify({}) == SIDE_EFFECT


def test_other_hints_do_not_grant_read_only() -> None:
    assert classify({"destructiveHint": False, "idempotentHint": True}) == SIDE_EFFECT
