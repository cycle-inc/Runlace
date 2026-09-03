from __future__ import annotations

import pytest

from runlace.naming import class_name, param_name, python_identifier, unmap_param_name


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("everything", "everything"),
        ("my-server", "my_server"),
        ("Weird  Name!", "Weird_Name"),
        ("2fast", "_2fast"),
        ("class", "class_"),
        ("match", "match_"),  # soft keyword
        ("---", None),
    ],
)
def test_python_identifier(raw: str, expected: str | None) -> None:
    assert python_identifier(raw) == expected


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("to", "to"),
        ("from", "from_"),  # the spec's worked example
        ("lambda", "lambda_"),
        ("content-type", None),
        ("", None),
    ],
)
def test_param_name(key: str, expected: str | None) -> None:
    assert param_name(key) == expected


def test_unmap_param_name_reverses_reserved_words() -> None:
    keys = ["from", "to"]
    assert unmap_param_name("from_", keys) == "from"
    assert unmap_param_name("to", keys) == "to"


def test_unmap_leaves_real_trailing_underscore_keys_alone() -> None:
    # A server that genuinely names a key "from_" must keep it.
    assert unmap_param_name("from_", ["from_", "to"]) == "from_"


@pytest.mark.parametrize(
    ("raw", "suffix", "expected"),
    [
        ("list_transactions", "", "ListTransactions"),
        ("echo", "Input", "EchoInput"),
        ("get-weather", "", "GetWeather"),
        ("2things", "", "T2things"),
    ],
)
def test_class_name(raw: str, suffix: str, expected: str) -> None:
    assert class_name(raw, suffix) == expected
