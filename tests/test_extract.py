"""Static tool extraction (D3 stage 3)."""

from __future__ import annotations

from runlace.extract import extract_tool_calls, unique_tools


def test_no_tool_calls() -> None:
    assert extract_tool_calls("def run(ctx):\n    return {}\n") == []


def test_one_call_per_site_with_its_line() -> None:
    code = (
        "def run(ctx):\n"
        "    a = ctx.pennylane.get_balance()\n"
        "    b = ctx.pennylane.get_balance()\n"
        "    ctx.gmail.send_email(to='x', subject='y', body='z')\n"
        "    return {}\n"
    )
    calls = extract_tool_calls(code)
    assert [(c.connector, c.method, c.line) for c in calls] == [
        ("pennylane", "get_balance", 2),
        ("pennylane", "get_balance", 3),
        ("gmail", "send_email", 4),
    ]


def test_unique_tools_deduplicates() -> None:
    code = (
        "def run(ctx):\n"
        "    ctx.pennylane.get_balance()\n"
        "    ctx.pennylane.get_balance()\n"
        "    ctx.gmail.send_email()\n"
        "    return {}\n"
    )
    assert unique_tools(extract_tool_calls(code)) == [
        ("gmail", "send_email"),
        ("pennylane", "get_balance"),
    ]


def test_calls_in_branches_loops_and_comprehensions_are_found() -> None:
    code = (
        "def run(ctx):\n"
        "    if ctx.inputs['x']:\n"
        "        ctx.gmail.send_email()\n"
        "    for _ in range(3):\n"
        "        ctx.pennylane.get_balance()\n"
        "    rows = [ctx.pennylane.list_transactions() for _ in range(2)]\n"
        "    return {'rows': rows}\n"
    )
    assert unique_tools(extract_tool_calls(code)) == [
        ("gmail", "send_email"),
        ("pennylane", "get_balance"),
        ("pennylane", "list_transactions"),
    ]


def test_calls_from_a_nested_function_are_found() -> None:
    """Dead-looking code still counts: extraction is about what *could* be called."""
    code = (
        "def run(ctx):\n"
        "    def inner():\n"
        "        ctx.gmail.send_email()\n"
        "    if ctx.inputs['send']:\n"
        "        inner()\n"
        "    return {}\n"
    )
    assert unique_tools(extract_tool_calls(code)) == [("gmail", "send_email")]


def test_ctx_inputs_is_not_a_tool_call() -> None:
    code = "def run(ctx):\n    return {'x': ctx.inputs['x']}\n"
    assert extract_tool_calls(code) == []


def test_attribute_calls_on_other_objects_are_ignored() -> None:
    code = (
        "import json\n"
        "def run(ctx):\n"
        "    text = json.dumps({})\n"
        "    return {'n': text.count('a')}\n"
    )
    assert extract_tool_calls(code) == []
