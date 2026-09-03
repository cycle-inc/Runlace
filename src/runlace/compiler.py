"""`create_workflow` as a compiler (D3).

Four stages, in order, stopping at the first that fails:

1. ``lint``      -- the ``ast`` rules in :mod:`runlace.lint`.
2. ``typecheck`` -- pyright, strict, against a private copy of ``runlace_types``
   in which ``Inputs`` and ``Output`` have been narrowed to this workflow's
   declared schemas.
3. ``extract``   -- read every ``ctx.X.Y(...)`` call off the AST and resolve it
   against the tools discovery recorded.
4. ``pin``       -- attach each tool's risk and schema hash, so a later run can
   tell whether the contract the workflow was compiled against still holds.

Nothing here writes to the Runlace home. Compilation happens in a temporary
directory and returns a verdict; persisting is :mod:`runlace.workflows`' job.
"""

from __future__ import annotations

import shutil
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .db import Connection
from .extract import ToolCall, extract_tool_calls
from .lint import lint
from .naming import python_identifier
from .stubs import WORKFLOW_TYPES_STUB, render_workflow_types
from .typecheck import Diagnostic, check_paths

STAGE_LINT = "lint"
STAGE_TYPECHECK = "typecheck"
STAGE_EXTRACT = "extract"


@dataclass(frozen=True)
class CompileError:
    """One reason a workflow was rejected, addressed to the agent that wrote it."""

    stage: str
    line: int | None
    message: str
    hint: str
    code: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "line": self.line,
            "message": self.message,
            "hint": self.hint,
            "code": self.code,
        }

    def __str__(self) -> str:
        where = f"line {self.line}: " if self.line else ""
        return f"[{self.stage}] {where}{self.message}"


@dataclass(frozen=True)
class PinnedTool:
    """A tool the workflow calls, with the contract it was compiled against."""

    connector: str  # verbatim connector name
    attr: str  # how it is spelled as ctx.<attr>
    tool: str  # verbatim MCP tool name
    method: str  # how it is spelled in the stubs
    risk: str
    schema_hash: str
    lines: tuple[int, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "connector": self.connector,
            "tool": self.tool,
            "risk": self.risk,
            "schema_hash": self.schema_hash,
        }


@dataclass
class CompileResult:
    ok: bool
    stage: str | None = None
    errors: list[CompileError] = field(default_factory=list[CompileError])
    tools_used: list[PinnedTool] = field(default_factory=list[PinnedTool])
    warnings: list[str] = field(default_factory=list[str])

    @property
    def has_side_effects(self) -> bool:
        return any(t.risk != "read_only" for t in self.tools_used)


def compile_workflow(
    conn: Connection,
    types_dir: Path,
    *,
    name: str,
    code: str,
    inputs_schema: dict[str, Any] | None,
    outputs_schema: dict[str, Any] | None = None,
) -> CompileResult:
    """Run the D3 pipeline. Never raises for bad input; it returns a verdict."""
    lint_errors = lint(code, outputs_declared=outputs_schema is not None)
    if lint_errors:
        return CompileResult(
            ok=False,
            stage=STAGE_LINT,
            errors=[
                CompileError(STAGE_LINT, e.line, e.message, e.hint, e.code)
                for e in lint_errors
            ],
        )

    typecheck_errors = _typecheck(
        types_dir,
        name=name,
        code=code,
        inputs_schema=inputs_schema,
        outputs_schema=outputs_schema,
    )
    if typecheck_errors:
        return CompileResult(ok=False, stage=STAGE_TYPECHECK, errors=typecheck_errors)

    return _extract_and_pin(conn, code)


# -- stage 2 ---------------------------------------------------------------


def _module_name(name: str) -> str:
    return python_identifier(name) or "workflow"


def _typecheck(
    types_dir: Path,
    *,
    name: str,
    code: str,
    inputs_schema: dict[str, Any] | None,
    outputs_schema: dict[str, Any] | None,
) -> list[CompileError]:
    if not types_dir.exists():
        return [
            CompileError(
                STAGE_TYPECHECK,
                None,
                f"the generated stub package is missing ({types_dir})",
                "Run `runlace init` to connect to your MCP servers and generate "
                "the types workflows are checked against.",
            )
        ]

    root = Path(tempfile.mkdtemp(prefix="runlace-compile-"))
    try:
        staged_types = root / types_dir.name
        shutil.copytree(types_dir, staged_types)
        (staged_types / WORKFLOW_TYPES_STUB).write_text(
            render_workflow_types(inputs_schema, outputs_schema), encoding="utf-8"
        )

        workflow_file = root / f"{_module_name(name)}.py"
        workflow_file.write_text(code, encoding="utf-8")

        result = check_paths(root, [workflow_file], types_dir=staged_types)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    if result.ok:
        return []
    if not result.errors:
        # pyright failed without diagnostics: not installed, timed out, crashed.
        return [
            CompileError(
                STAGE_TYPECHECK,
                None,
                result.summary,
                "This is a problem with the Runlace installation, not with the "
                "workflow. Check that pyright is installed and try again.",
            )
        ]
    return [
        CompileError(
            STAGE_TYPECHECK,
            diagnostic.line,
            diagnostic.message,
            _typecheck_hint(diagnostic.rule, diagnostic.message),
            diagnostic.rule,
        )
        for diagnostic in _without_cascades(result.errors)
    ]


# Strict mode reports these alongside the error that caused them: an unknown
# attribute is also an unknown type. Reporting both makes the agent read two
# messages to learn one thing.
_CASCADE_RULES = frozenset(
    {
        "reportUnknownMemberType",
        "reportUnknownVariableType",
        "reportUnknownArgumentType",
        "reportUnknownParameterType",
    }
)


def _without_cascades(diagnostics: list[Diagnostic]) -> list[Diagnostic]:
    """Drop "type is unknown" noise from lines that already have a real error."""
    explained = {d.line for d in diagnostics if d.rule not in _CASCADE_RULES}
    kept = [
        d for d in diagnostics if d.rule not in _CASCADE_RULES or d.line not in explained
    ]
    return kept or diagnostics


_TYPECHECK_HINTS = {
    "reportAttributeAccessIssue": (
        "That connector or tool does not exist. Call `get_skill` for the list of "
        "connectors and their tools, and check the spelling."
    ),
    "reportCallIssue": (
        "Check the tool's parameters against its stub: arguments are "
        "keyword-only, and required ones cannot be omitted."
    ),
    "reportArgumentType": (
        "The value's type does not match the tool's schema. Convert it, or narrow "
        "the result you took it from."
    ),
    "reportTypedDictNotRequiredAccess": (
        "That key is optional in the schema. Use `.get(...)` or declare it "
        "required in inputs_schema."
    ),
    "reportReturnType": (
        "The value you return does not match the outputs_schema you declared. "
        "Fix one or the other so they agree."
    ),
}

_TYPECHECK_DEFAULT_HINT = (
    "pyright checked the workflow in strict mode against the generated stubs. "
    "Fix the reported line. Strict mode also refuses values whose type it "
    "cannot infer: annotate the accumulator you build in a loop, or use a "
    "comprehension."
)


def _typecheck_hint(rule: str | None, message: str) -> str:
    if 'is not assignable to return type "Output"' in message and "invariant" in message:
        # The list is built in a variable, so pyright infers
        # `list[dict[str, ...]]` and a list of TypedDicts is not that -- lists
        # are invariant. Built inside the `return`, the declared Output gives
        # each dict literal its expected type and the same code is accepted.
        # The generic "fix one or the other" hint sends the agent rewriting a
        # schema that was already right.
        return (
            "Your value and the outputs_schema agree; this is a variance "
            "artifact. Build the list inside the `return` statement rather than "
            "in a variable first, so pyright checks each item against the "
            "schema instead of inferring a plain list of dicts."
        )
    if 'not a defined key in "Inputs"' in message:
        return (
            "That key is not in the inputs_schema you declared. Add it to "
            "inputs_schema, or read a key you did declare."
        )
    if 'not a defined key in "Output"' in message:
        return _TYPECHECK_HINTS["reportReturnType"]
    return _TYPECHECK_HINTS.get(rule or "", _TYPECHECK_DEFAULT_HINT)


# -- stages 3 and 4 --------------------------------------------------------


def _extract_and_pin(conn: Connection, code: str) -> CompileResult:
    calls = extract_tool_calls(code)
    lines_by_pair: dict[tuple[str, str], list[int]] = defaultdict(list)
    for call in calls:
        lines_by_pair[(call.connector, call.method)].append(call.line)

    known_attrs = {
        str(row["attr"]) for row in conn.execute("SELECT attr FROM connectors")
    }

    errors: list[CompileError] = []
    pinned: list[PinnedTool] = []
    for (attr, method), lines in sorted(lines_by_pair.items()):
        row = conn.execute(
            """
            SELECT c.name AS connector, t.name AS tool, t.method AS method,
                   t.risk AS risk, t.schema_hash AS schema_hash
            FROM tools t
            JOIN connectors c ON c.name = t.connector
            WHERE c.attr = ? AND t.method = ?
            """,
            (attr, method),
        ).fetchone()
        if row is None:
            errors.append(_unresolved(attr, method, lines[0], known_attrs))
            continue
        pinned.append(
            PinnedTool(
                connector=str(row["connector"]),
                attr=attr,
                tool=str(row["tool"]),
                method=str(row["method"]),
                risk=str(row["risk"]),
                schema_hash=str(row["schema_hash"]),
                lines=tuple(sorted(lines)),
            )
        )

    if errors:
        return CompileResult(ok=False, stage=STAGE_EXTRACT, errors=errors)

    pinned.sort(key=lambda t: (t.connector, t.tool))
    return CompileResult(ok=True, tools_used=pinned, warnings=_warnings(calls, pinned))


def _unresolved(
    attr: str, method: str, line: int, known_attrs: set[str]
) -> CompileError:
    if attr not in known_attrs:
        return CompileError(
            STAGE_EXTRACT,
            line,
            f"there is no connector called `{attr}`",
            "Call `get_skill` for the connectors this machine actually has. "
            "Connectors that failed to connect during `runlace init` have no "
            "tools and cannot be used.",
            "unknown-connector",
        )
    return CompileError(
        STAGE_EXTRACT,
        line,
        f"connector `{attr}` has no tool called `{method}`",
        "Call `get_skill` for the tools this connector exposes. The stubs are "
        "the source of truth; if the tool was added recently, run "
        "`runlace init` again.",
        "unknown-tool",
    )


def _warnings(calls: list[ToolCall], pinned: list[PinnedTool]) -> list[str]:
    side_effects = [t for t in pinned if t.risk != "read_only"]
    warnings: list[str] = []
    if side_effects:
        names = ", ".join(f"{t.connector}.{t.tool}" for t in side_effects)
        warnings.append(
            f"This workflow performs side effects ({names}). Running it will "
            f"require confirm=True."
        )
    if not calls:
        warnings.append(
            "This workflow calls no tools. It will run, but it only computes."
        )
    return warnings
