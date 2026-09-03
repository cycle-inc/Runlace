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
    E_CTX_ANNOTATION,
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
    E_RUN_DECORATED,
    E_RUN_SIGNATURE,
    E_STUB_SUBMODULE,
    E_SYNTAX,
    E_UNKNOWN_CONNECTOR,
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


def test_reaching_into_the_stub_package_is_rejected() -> None:
    """`runlace_types` is `.pyi` files. Only its top level exists at run time."""
    errors = lint(
        workflow(
            "pass",
            preamble="\nfrom runlace_types.connectors import pennylane",
        )
    )
    assert [e.code for e in errors] == [E_STUB_SUBMODULE]
    assert "runlace_types.connectors" in errors[0].message


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


def test_a_workflow_cannot_bring_its_own_output_type() -> None:
    """Otherwise D7 checks the return value against a shape nobody agreed to.

    pyright is perfectly happy with a locally defined `Output`, so the outputs
    schema goes unenforced until Pydantic rejects the result -- after the side
    effects have happened.
    """
    code = (
        "from runlace_types import Ctx\n"
        "from typing import TypedDict\n\n\n"
        "class Output(TypedDict):\n    count: str\n\n\n"
        'def run(ctx: Ctx) -> Output:\n    return {"count": "x"}\n'
    )
    errors = lint(code, outputs_declared=True)
    assert [e.code for e in errors] == [E_OUTPUT_ANNOTATION]
    assert errors[0].line == 5  # the class, which is what has to go
    assert "shadowing" in errors[0].message


def test_the_output_type_has_to_be_imported() -> None:
    code = "from runlace_types import Ctx\n\n\ndef run(ctx: Ctx) -> Output:\n    return {}\n"
    errors = lint(code, outputs_declared=True)
    assert [e.code for e in errors] == [E_OUTPUT_ANNOTATION]
    assert "never imported" in errors[0].message


# -- ctx, and the connector index -------------------------------------------


def test_the_ctx_parameter_has_to_be_annotated() -> None:
    """One missing annotation is nine unreadable pyright errors, or one of these."""
    code = (
        "from runlace_types import Ctx\n\n\n"
        "def run(ctx) -> dict[str, object]:\n    return {}\n"
    )
    errors = lint(code)
    assert [e.code for e in errors] == [E_CTX_ANNOTATION]
    assert "no type annotation" in errors[0].message


def test_the_ctx_type_has_to_be_imported() -> None:
    errors = lint("def run(ctx: Ctx) -> dict[str, object]:\n    return {}\n")
    assert [e.code for e in errors] == [E_CTX_ANNOTATION]
    assert "never imported" in errors[0].message


def test_a_workflow_cannot_bring_its_own_ctx_type() -> None:
    code = (
        "from typing import Any\n\n\n"
        "class Ctx:\n    everything: Any\n\n\n"
        "def run(ctx: Ctx) -> dict[str, object]:\n    return {}\n"
    )
    errors = lint(code)
    assert [e.code for e in errors] == [E_CTX_ANNOTATION]
    assert errors[0].line == 4  # the class, which is what has to go


def test_annotating_ctx_with_something_else_is_rejected() -> None:
    code = "from runlace_types import Ctx\n\n\ndef run(ctx: object) -> dict[str, object]:\n    return {}\n"
    errors = lint(code)
    assert [e.code for e in errors] == [E_CTX_ANNOTATION]
    assert "must be annotated `Ctx`" in errors[0].message


CONNECTORS = {"everything": ["echo", "get_sum"], "gmail": ["send_email"]}


def test_a_tool_used_where_a_connector_belongs_is_named_as_such() -> None:
    """The mistake a model makes after reading a flat index of tools.

    The generic hint tells it to write `ctx.echo.<tool>(...)`, which is the same
    mistake with an extra level on it.
    """
    errors = lint(workflow('ctx.echo(message="hi")'), connectors=CONNECTORS)
    assert [e.code for e in errors] == [E_UNKNOWN_CONNECTOR]
    assert "`echo` is a tool, not a connector" in errors[0].message
    assert "ctx.everything.echo(...)" in errors[0].hint


def test_an_ambiguous_tool_name_gets_no_suggestion() -> None:
    """Guessing which server was meant would be the same error, better dressed."""
    both = {"a": ["send"], "b": ["send"]}
    errors = lint(workflow("ctx.send()"), connectors=both)
    assert [e.code for e in errors] == [E_CTX_TOOL_NOT_CALLED]
    assert "Connected on this machine: a, b." in errors[0].hint


def test_without_a_connector_index_the_hint_is_the_generic_one() -> None:
    errors = lint(workflow('ctx.echo(message="hi")'))
    assert [e.code for e in errors] == [E_CTX_TOOL_NOT_CALLED]
    assert "Connected on this machine" not in errors[0].hint


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


def test_output_must_be_imported_even_without_an_outputs_schema() -> None:
    """`-> Output` is legal there; an `Output` that came from nowhere is not."""
    code = "from runlace_types import Ctx\n\n\ndef run(ctx: Ctx) -> Output:\n    return {}\n"
    errors = lint(code)
    assert [e.code for e in errors] == [E_OUTPUT_ANNOTATION]
    assert "never imported" in errors[0].message


def test_importing_output_without_an_outputs_schema_is_fine() -> None:
    code = (
        "from runlace_types import Ctx, Output\n\n\n"
        "def run(ctx: Ctx) -> Output:\n    return {}\n"
    )
    assert lint(code) == []


# -- decorators --------------------------------------------------------------


def test_a_decorator_on_run_is_rejected() -> None:
    """`@workflow` is the habit a model brings from every other agent library."""
    code = (
        "from runlace_types import Ctx\n\n\n"
        "@workflow\n"
        "def run(ctx: Ctx) -> dict[str, object]:\n    return {}\n"
    )
    errors = lint(code)
    assert [e.code for e in errors] == [E_RUN_DECORATED]
    assert errors[0].line == 4
    assert "not a framework" in errors[0].hint


def test_every_decorator_on_run_is_named() -> None:
    """Reporting one of two leaves the second as a pyright error next round."""
    code = (
        "from runlace_types import Ctx\n\n\n"
        "@workflow\n@traced\n"
        "def run(ctx: Ctx) -> dict[str, object]:\n    return {}\n"
    )
    assert [e.line for e in lint(code)] == [4, 5]


def test_a_decorator_elsewhere_is_none_of_our_business() -> None:
    """Only `run` is the entry point; a helper may decorate itself freely."""
    code = (
        "from runlace_types import Ctx\nimport dataclasses\n\n\n"
        "@dataclasses.dataclass\nclass Row:\n    n: int\n\n\n"
        "def run(ctx: Ctx) -> dict[str, object]:\n    return {}\n"
    )
    assert lint(code) == []
