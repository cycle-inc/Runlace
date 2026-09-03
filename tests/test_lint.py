"""The ast lint (D3 stage 1).

The forbidden-pattern table is the milestone's acceptance criterion: every
pattern D3 names must be rejected, and each must be rejected with its own error
rather than a shared "lint failed".
"""

from __future__ import annotations

import pytest

from runlace import lint as lint_module
from runlace.lint import (
    E_ASYNC,
    E_CTX_ESCAPE,
    E_CTX_REBOUND,
    E_CTX_TOOL_NOT_CALLED,
    E_DUNDER_ACCESS,
    E_DYNAMIC_ACCESS,
    E_FORBIDDEN_CALL,
    E_FORBIDDEN_IMPORT,
    E_IMPORT_NOT_ALLOWED,
    E_MISSING_RUN,
    E_OUTPUT_ANNOTATION,
    E_RELATIVE_IMPORT,
    E_RETURN_ANNOTATION,
    E_RUN_SIGNATURE,
    E_SYNTAX,
    lint,
)

HEADER = "from runlace_types import Ctx\n"


def workflow(body: str, *, preamble: str = "") -> str:
    """A minimal valid workflow with `body` inside `run`."""
    indented = "\n".join(f"    {line}" if line else "" for line in body.splitlines())
    return (
        f"{HEADER}{preamble}\n\n"
        f"def run(ctx: Ctx) -> dict[str, object]:\n{indented}\n    return {{}}\n"
    )


def test_a_plain_workflow_passes() -> None:
    code = workflow("total = ctx.pennylane.get_balance()\nprint_me = total")
    assert lint(code) == []


def test_allowed_imports_pass() -> None:
    code = workflow("pass", preamble="\nimport json\nimport datetime\nfrom typing import Any")
    assert lint(code) == []


# -- one fixture per forbidden pattern D3 names ----------------------------

FORBIDDEN_PATTERNS: list[tuple[str, str, str]] = [
    ("subprocess", "import subprocess", E_FORBIDDEN_IMPORT),
    ("os", "import os", E_FORBIDDEN_IMPORT),
    ("sys", "import sys", E_FORBIDDEN_IMPORT),
    ("socket", "import socket", E_FORBIDDEN_IMPORT),
    ("http", "import http.client", E_FORBIDDEN_IMPORT),
    ("urllib", "from urllib.request import urlopen", E_FORBIDDEN_IMPORT),
    ("requests", "import requests", E_FORBIDDEN_IMPORT),
    ("httpx", "import httpx", E_FORBIDDEN_IMPORT),
    ("aiohttp", "import aiohttp", E_FORBIDDEN_IMPORT),
    ("importlib", "import importlib", E_FORBIDDEN_IMPORT),
]


@pytest.mark.parametrize(("label", "line", "code"), FORBIDDEN_PATTERNS)
def test_forbidden_imports_are_rejected(label: str, line: str, code: str) -> None:
    errors = lint(workflow("pass", preamble=f"\n{line}"))
    assert [e.code for e in errors] == [code]
    assert label in errors[0].message


def test_a_submodule_import_is_rejected_by_its_root() -> None:
    errors = lint(workflow("pass", preamble="\nfrom os import path"))
    assert [e.code for e in errors] == [E_FORBIDDEN_IMPORT]
    assert "`os`" in errors[0].message


def test_every_import_d3_forbids_is_covered() -> None:
    """Guards the table above against drifting from the module's own list."""
    covered = {label.split(".")[0] for label, _, _ in FORBIDDEN_PATTERNS}
    assert covered == set(lint_module.FORBIDDEN_IMPORTS)


FORBIDDEN_CALLS: list[tuple[str, str, str]] = [
    ("open", 'data = open("/etc/passwd")', E_FORBIDDEN_CALL),
    ("exec", 'exec("1")', E_FORBIDDEN_CALL),
    ("eval", 'value = eval("1")', E_FORBIDDEN_CALL),
    ("__import__", '__import__("os")', E_FORBIDDEN_CALL),
    ("getattr", 'tool = getattr(ctx, "gmail")', E_DYNAMIC_ACCESS),
    ("vars", "table = vars(ctx)", E_DYNAMIC_ACCESS),
    ("setattr", 'setattr(ctx, "x", 1)', E_DYNAMIC_ACCESS),
    ("globals", "table = globals()", E_DYNAMIC_ACCESS),
]


@pytest.mark.parametrize(("label", "line", "code"), FORBIDDEN_CALLS)
def test_forbidden_calls_are_rejected(label: str, line: str, code: str) -> None:
    errors = lint(workflow(line))
    assert [e.code for e in errors] == [code]
    assert label in errors[0].message


def test_ctx_dunder_access_is_rejected() -> None:
    errors = lint(workflow("table = ctx.__dict__"))
    assert [e.code for e in errors] == [E_DUNDER_ACCESS]
    assert "__dict__" in errors[0].message


def test_async_run_is_rejected() -> None:
    code = f"{HEADER}\n\nasync def run(ctx: Ctx) -> dict[str, object]:\n    return {{}}\n"
    errors = lint(code)
    assert E_ASYNC in [e.code for e in errors]
    assert "async def run" in " ".join(e.message for e in errors)


def test_await_is_rejected() -> None:
    code = (
        f"{HEADER}\n\ndef run(ctx: Ctx) -> dict[str, object]:\n"
        f"    value = await ctx.pennylane.get_balance()\n    return {{}}\n"
    )
    # `ast.parse` accepts this -- "await outside async" is a compile-time error,
    # not a parse-time one -- so the lint has to catch it itself.
    assert [e.code for e in lint(code)] == [E_ASYNC]


def test_relative_imports_are_rejected() -> None:
    errors = lint(workflow("pass", preamble="\nfrom . import helpers"))
    assert [e.code for e in errors] == [E_RELATIVE_IMPORT]


def test_imports_off_the_allowlist_are_rejected() -> None:
    errors = lint(workflow("pass", preamble="\nimport pandas"))
    assert [e.code for e in errors] == [E_IMPORT_NOT_ALLOWED]
    assert "pandas" in errors[0].message


# -- the ctx rules that keep extraction sound ------------------------------


def test_passing_ctx_to_a_helper_is_rejected() -> None:
    code = (
        f"{HEADER}\n\ndef helper(c: Ctx) -> object:\n"
        f"    return c.pennylane.get_balance()\n\n\n"
        f"def run(ctx: Ctx) -> dict[str, object]:\n"
        f"    return {{'balance': helper(ctx)}}\n"
    )
    assert [e.code for e in lint(code)] == [E_CTX_ESCAPE]


def test_rebinding_ctx_is_rejected() -> None:
    errors = lint(workflow("ctx = None"))
    assert [e.code for e in errors] == [E_CTX_REBOUND]


def test_storing_a_connector_in_a_variable_is_rejected() -> None:
    errors = lint(workflow("mail = ctx.gmail"))
    assert [e.code for e in errors] == [E_CTX_TOOL_NOT_CALLED]
    assert "ctx.gmail" in errors[0].message


def test_referencing_a_tool_without_calling_it_is_rejected() -> None:
    errors = lint(workflow("send = ctx.gmail.send_email"))
    assert [e.code for e in errors] == [E_CTX_TOOL_NOT_CALLED]
    assert "ctx.gmail.send_email" in errors[0].message


def test_reading_ctx_inputs_is_allowed() -> None:
    assert lint(workflow('who = ctx.inputs["to"]\nalso = who')) == []


# -- the run() contract ----------------------------------------------------


def test_a_file_with_no_run_is_rejected() -> None:
    errors = lint(f"{HEADER}\n\ndef helper() -> int:\n    return 1\n")
    assert [e.code for e in errors] == [E_MISSING_RUN]


def test_run_with_extra_parameters_is_rejected() -> None:
    code = f"{HEADER}\n\ndef run(ctx: Ctx, extra: int) -> dict[str, object]:\n    return {{}}\n"
    assert [e.code for e in lint(code)] == [E_RUN_SIGNATURE]


def test_run_must_name_its_argument_ctx() -> None:
    code = f"{HEADER}\n\ndef run(context: Ctx) -> dict[str, object]:\n    return {{}}\n"
    errors = lint(code)
    assert [e.code for e in errors] == [E_RUN_SIGNATURE]
    assert "context" in errors[0].message


def test_run_needs_a_return_annotation() -> None:
    code = f"{HEADER}\n\ndef run(ctx: Ctx):\n    return {{}}\n"
    assert [e.code for e in lint(code)] == [E_RETURN_ANNOTATION]


def test_declaring_outputs_requires_the_output_annotation() -> None:
    code = f"{HEADER}\n\ndef run(ctx: Ctx) -> dict[str, object]:\n    return {{}}\n"
    assert lint(code, outputs_declared=False) == []
    errors = lint(code, outputs_declared=True)
    assert [e.code for e in errors] == [E_OUTPUT_ANNOTATION]
    assert errors[0].line == 4  # the annotation, not the file


def test_output_annotation_satisfies_the_outputs_rule() -> None:
    code = (
        "from runlace_types import Ctx, Output\n\n\n"
        "def run(ctx: Ctx) -> Output:\n    return {}\n"
    )
    assert lint(code, outputs_declared=True) == []


# -- shape of the errors ---------------------------------------------------


def test_syntax_errors_report_their_line() -> None:
    errors = lint("def run(ctx:\n")
    assert [e.code for e in errors] == [E_SYNTAX]
    assert errors[0].line == 1


def test_every_error_carries_a_line_and_a_hint() -> None:
    errors = lint(workflow('open("/tmp/x")', preamble="\nimport os"))
    assert len(errors) == 2
    assert all(e.line > 0 and e.hint for e in errors)
    assert [e.line for e in errors] == sorted(e.line for e in errors)


def test_forbidden_patterns_produce_distinct_errors() -> None:
    """No two fixtures may be rejected with the same message."""
    fixtures = [line for _, line, _ in FORBIDDEN_PATTERNS]
    messages = {lint(workflow("pass", preamble=f"\n{line}"))[0].message for line in fixtures}
    assert len(messages) == len(fixtures)

    call_messages = {lint(workflow(line))[0].message for _, line, _ in FORBIDDEN_CALLS}
    assert len(call_messages) == len(FORBIDDEN_CALLS)
