"""Stage 1 of the compiler (D3): the ``ast`` lint.

This runs before pyright and decides whether the file is the kind of program
Runlace is willing to store at all: no I/O, no dynamic attribute access, and
``ctx`` used in a way that makes stage 3's static extraction sound.

Every rejection carries a distinct code, the line it happened on, and a hint
saying what to do instead, because the reader is an LLM that has to fix it.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# Third-party imports a workflow may use. Extending this list is a v2 policy
# decision, not a code change.
ALLOWED_IMPORTS = frozenset(
    {
        "json",
        "datetime",
        "re",
        "math",
        "collections",
        "itertools",
        "statistics",
        "decimal",
        "dataclasses",
        "typing",
    }
)

# The generated stub package, which is where `Ctx` comes from.
STUB_PACKAGE = "runlace_types"

# Named in D3. These get a sharper message than "not on the allowlist".
FORBIDDEN_IMPORTS = frozenset(
    {
        "subprocess",
        "os",
        "sys",
        "socket",
        "http",
        "urllib",
        "requests",
        "httpx",
        "aiohttp",
        "importlib",
    }
)

FORBIDDEN_CALLS = frozenset({"open", "exec", "eval", "__import__"})

# D3 forbids dynamic access on ctx and names getattr(ctx, ...), ctx.__dict__ and
# vars(ctx). Detecting those spellings alone is not sound -- `c = ctx` defeats
# it -- so the builtins that perform dynamic attribute access are refused
# outright, and `ctx` is separately barred from escaping into another name.
DYNAMIC_ACCESS_CALLS = frozenset(
    {"getattr", "setattr", "delattr", "vars", "globals", "locals"}
)

CTX_PARAM = "ctx"
RUN_FUNCTION = "run"

# The one `ctx` attribute that is not a connector (D7). Both the lint and the
# extractor have to know it, and they have to agree: if only one of them does,
# `ctx.inputs.get("branch", "main")` lints clean and then gets extracted as a
# call to a connector named `inputs`.
INPUTS_ATTR = "inputs"

# The other `ctx` attribute that is not a connector: `ctx.ai(system=..., user=...)`
# asks the configured model a question. One attribute deep, not two, because
# there is exactly one thing to do with a model and no server to name.
AI_ATTR = "ai"

E_SYNTAX = "syntax-error"
E_FORBIDDEN_IMPORT = "forbidden-import"
E_IMPORT_NOT_ALLOWED = "import-not-allowed"
E_RELATIVE_IMPORT = "relative-import"
E_STUB_SUBMODULE = "stub-submodule-import"
E_FORBIDDEN_CALL = "forbidden-call"
E_DYNAMIC_ACCESS = "dynamic-attribute-access"
E_DUNDER_ACCESS = "dunder-access"
E_ASYNC = "async-not-supported"
E_MISSING_RUN = "missing-run"
E_RUN_SIGNATURE = "bad-run-signature"
E_CTX_REBOUND = "ctx-rebound"
E_CTX_ESCAPE = "ctx-escape"
E_CTX_TOOL_NOT_CALLED = "ctx-tool-not-called"
E_RETURN_ANNOTATION = "missing-return-annotation"
E_OUTPUT_ANNOTATION = "bad-output-annotation"
E_CTX_ANNOTATION = "bad-ctx-annotation"
E_UNKNOWN_CONNECTOR = "unknown-connector"
E_RUN_DECORATED = "run-decorated"

OUTPUT_TYPE = "Output"
CTX_TYPE = "Ctx"


@dataclass(frozen=True)
class LintError:
    line: int
    code: str
    message: str
    hint: str

    def to_json(self) -> dict[str, Any]:
        return {
            "line": self.line,
            "code": self.code,
            "message": self.message,
            "hint": self.hint,
        }


def lint(
    code: str,
    *,
    outputs_declared: bool = False,
    connectors: Mapping[str, Sequence[str]] | None = None,
) -> list[LintError]:
    """Check a workflow file. An empty list means it passed.

    ``outputs_declared`` says whether the workflow came with an
    ``outputs_schema``; if it did, D7 requires ``run`` to be annotated
    ``-> Output`` so pyright can check the return value against it.

    ``connectors`` maps ``ctx.<attr>`` to the tool methods spelled on it, as
    :func:`runlace.db.method_index` returns it. It changes no verdict -- pyright
    would reject an unknown connector a stage later regardless -- and only
    sharpens the message: "`echo` is a tool on `everything`" is a fix, where
    "write `ctx.echo.<tool>(...)`" is a wrong turn confidently signposted.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [
            LintError(
                line=exc.lineno or 1,
                code=E_SYNTAX,
                message=f"the file is not valid Python: {exc.msg}",
                hint="Fix the syntax error and submit the workflow again.",
            )
        ]

    checker = _Checker(tree, outputs_declared=outputs_declared, connectors=connectors)
    checker.run()
    return sorted(checker.errors, key=lambda e: (e.line, e.code))


def _is_dynamic_access_call(node: ast.AST | None) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in DYNAMIC_ACCESS_CALLS
    )


class _Checker:
    def __init__(
        self,
        tree: ast.Module,
        *,
        outputs_declared: bool,
        connectors: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        self.tree = tree
        self.outputs_declared = outputs_declared
        self.connectors = connectors
        self.errors: list[LintError] = []
        self.parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                self.parents[child] = node

    def _imports_from_stubs(self, name: str) -> bool:
        return any(
            isinstance(node, ast.ImportFrom)
            and node.module == STUB_PACKAGE
            and not node.level
            and any(a.name == name and a.asname is None for a in node.names)
            for node in self.tree.body
        )

    def add(self, node: ast.AST, code: str, message: str, hint: str) -> None:
        self.errors.append(
            LintError(getattr(node, "lineno", 1), code, message, hint)
        )

    def run(self) -> None:
        self._check_run_function()
        for node in ast.walk(self.tree):
            self._check_imports(node)
            self._check_calls(node)
            self._check_async(node)
            self._check_attributes(node)
            self._check_ctx_names(node)

    # -- the entry point -------------------------------------------------

    def _check_run_function(self) -> None:
        functions = [
            n
            for n in self.tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == RUN_FUNCTION
        ]
        if not functions:
            self.errors.append(
                LintError(
                    line=self.tree.body[0].lineno if self.tree.body else 1,
                    code=E_MISSING_RUN,
                    message=f"the workflow does not define a top-level "
                    f"`{RUN_FUNCTION}` function",
                    hint=f"Add `def {RUN_FUNCTION}({CTX_PARAM}: Ctx) -> "
                    f"dict[str, object]:` as the entry point.",
                )
            )
            return

        run = functions[-1]
        self._check_not_decorated(run)
        args = run.args
        if (
            len(args.posonlyargs) + len(args.args) != 1
            or args.vararg is not None
            or args.kwarg is not None
            or args.kwonlyargs
        ):
            self.add(
                run,
                E_RUN_SIGNATURE,
                f"`{RUN_FUNCTION}` must take exactly one argument",
                f"Use `def {RUN_FUNCTION}({CTX_PARAM}: Ctx) -> dict[str, object]:`. "
                f"Anything that varies between runs belongs in inputs_schema, "
                f"not in extra parameters.",
            )
            return

        only = (args.posonlyargs + args.args)[0]
        if only.arg != CTX_PARAM:
            self.add(
                run,
                E_RUN_SIGNATURE,
                f"`{RUN_FUNCTION}` must name its argument `{CTX_PARAM}`, "
                f"not `{only.arg}`",
                f"Use `def {RUN_FUNCTION}({CTX_PARAM}: Ctx)`. Tool calls are found "
                f"by reading `{CTX_PARAM}.<connector>.<tool>(...)` out of the code, "
                f"so the name is fixed.",
            )

        self._check_ctx_annotation(only)
        self._check_return_annotation(run)

    def _check_not_decorated(
        self, run: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        """No decorator on `run`. There is no framework to register with.

        A model that has seen other agent libraries writes `@workflow` above
        `run`, and every decorator it could invent is a name the allowlist does
        not let it import. Left to pyright that is two errors -- "workflow is
        not defined" and "untyped function decorator obscures type of function"
        -- neither of which says that the line should not be there at all.
        """
        for decorator in run.decorator_list:
            self.add(
                decorator,
                E_RUN_DECORATED,
                f"`{RUN_FUNCTION}` cannot be decorated",
                f"Delete the decorator. Runlace is not a framework you register "
                f"with: `{RUN_FUNCTION}` is found by name, and the tools it uses "
                f"are read off `{CTX_PARAM}.<connector>.<tool>(...)` in its body.",
            )

    def _check_ctx_annotation(self, parameter: ast.arg) -> None:
        """`ctx` has to be annotated `Ctx`, and `Ctx` has to be the imported one.

        pyright would catch all of this a stage later, but not legibly: one
        missing import line comes back as nine errors, every one of them a
        variant of "type of X is unknown", none of them saying which line to
        write. Here it is one error that names it.
        """
        annotation = parameter.annotation
        if annotation is None:
            self.add(
                parameter,
                E_CTX_ANNOTATION,
                f"`{CTX_PARAM}` has no type annotation",
                f"Write `def {RUN_FUNCTION}({CTX_PARAM}: {CTX_TYPE})` and "
                f"`from {STUB_PACKAGE} import {CTX_TYPE}` above it. Without the "
                f"annotation pyright knows nothing about your connectors and "
                f"rejects every line that touches one.",
            )
            return

        if not (isinstance(annotation, ast.Name) and annotation.id == CTX_TYPE):
            self.add(
                annotation,
                E_CTX_ANNOTATION,
                f"`{CTX_PARAM}` must be annotated `{CTX_TYPE}`",
                f"Write `def {RUN_FUNCTION}({CTX_PARAM}: {CTX_TYPE})`. "
                f"`{CTX_TYPE}` is generated from the servers you are connected "
                f"to; it is what gives `{CTX_PARAM}.<connector>.<tool>` a type.",
            )
            return

        own = self._own_binding(CTX_TYPE)
        if own is not None:
            self.add(
                own,
                E_CTX_ANNOTATION,
                f"`{CTX_TYPE}` is defined in the workflow, shadowing the "
                f"generated one",
                f"Delete it and write `from {STUB_PACKAGE} import {CTX_TYPE}`. "
                f"Runlace generates `{CTX_TYPE}` from your live connectors; a "
                f"local one describes servers nobody is connected to.",
            )
            return

        if not self._imports_from_stubs(CTX_TYPE):
            self.add(
                self.tree.body[0] if self.tree.body else self.tree,
                E_CTX_ANNOTATION,
                f"`{CTX_TYPE}` is used but never imported from `{STUB_PACKAGE}`",
                f"Add `from {STUB_PACKAGE} import {CTX_TYPE}` as the first line "
                f"of the file.",
            )

    def _check_return_annotation(
        self, run: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        returns = run.returns
        if returns is None:
            self.add(
                run,
                E_RETURN_ANNOTATION,
                f"`{RUN_FUNCTION}` has no return type annotation",
                f"Annotate it: `-> {OUTPUT_TYPE}` when the workflow declares an "
                f"outputs_schema, `-> dict[str, object]` otherwise.",
            )
            return

        annotated_output = isinstance(returns, ast.Name) and returns.id == OUTPUT_TYPE

        if not self.outputs_declared:
            # `-> Output` without an outputs_schema is legal -- the persistent
            # stub's `Output` is permissive -- but it still has to be the
            # imported one, or pyright answers with "Output is not defined" and
            # no idea where it should have come from.
            if annotated_output:
                self._check_output_is_the_generated_one()
            return

        if not annotated_output:
            self.add(
                returns,
                E_OUTPUT_ANNOTATION,
                f"the workflow declares an outputs_schema, so `{RUN_FUNCTION}` must "
                f"be annotated `-> {OUTPUT_TYPE}`",
                f"Write `from {STUB_PACKAGE} import Ctx, {OUTPUT_TYPE}` and "
                f"`def {RUN_FUNCTION}({CTX_PARAM}: Ctx) -> {OUTPUT_TYPE}:`. "
                f"`{OUTPUT_TYPE}` is generated from your outputs_schema, so pyright "
                f"checks the value you return against it.",
            )
            return

        self._check_output_is_the_generated_one()

    def _check_output_is_the_generated_one(self) -> None:
        """`-> Output` has to mean *our* Output, or D7 checks nothing.

        A workflow that writes its own `class Output(TypedDict)` annotates `run`
        with a type it invented, pyright happily checks the return value against
        that, and the outputs_schema is never enforced until Pydantic rejects
        the result after the side effects have already happened.
        """
        own = self._own_binding(OUTPUT_TYPE)
        if own is not None:
            self.add(
                own,
                E_OUTPUT_ANNOTATION,
                f"`{OUTPUT_TYPE}` is defined in the workflow, shadowing the one "
                f"generated from outputs_schema",
                f"Delete it and write `from {STUB_PACKAGE} import Ctx, "
                f"{OUTPUT_TYPE}`. Runlace generates `{OUTPUT_TYPE}` from the "
                f"outputs_schema you declared; a local one means pyright checks "
                f"your return value against a shape nobody agreed to.",
            )
            return

        if not self._imports_from_stubs(OUTPUT_TYPE):
            self.add(
                self.tree.body[0] if self.tree.body else self.tree,
                E_OUTPUT_ANNOTATION,
                f"`{OUTPUT_TYPE}` is used but never imported from `{STUB_PACKAGE}`",
                f"Add `from {STUB_PACKAGE} import Ctx, {OUTPUT_TYPE}` at the top "
                f"of the file.",
            )

    def _own_binding(self, name: str) -> ast.AST | None:
        """A top-level statement in the workflow that binds ``name`` itself."""
        for node in self.tree.body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == name:
                    return node
            elif isinstance(node, ast.Assign):
                if any(
                    isinstance(t, ast.Name) and t.id == name for t in node.targets
                ):
                    return node
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name) and node.target.id == name:
                    return node
        return None

    # -- imports ---------------------------------------------------------

    def _check_imports(self, node: ast.AST) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                self._check_module(node, alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                self.add(
                    node,
                    E_RELATIVE_IMPORT,
                    "relative imports are not allowed",
                    "A workflow is a single file. Import only from the allowlist: "
                    + ", ".join(sorted(ALLOWED_IMPORTS)),
                )
                return
            self._check_module(node, node.module or "")

    def _check_module(self, node: ast.AST, dotted: str) -> None:
        root = dotted.split(".")[0]
        if root == STUB_PACKAGE:
            if dotted != STUB_PACKAGE:
                # The generated package is stubs only: `.pyi` files with no code
                # behind them. Its top-level names exist at run time because the
                # runner provides them; nothing deeper can.
                self.add(
                    node,
                    E_STUB_SUBMODULE,
                    f"`{dotted}` cannot be imported at run time",
                    f"Import only from the top level: "
                    f"`from {STUB_PACKAGE} import Ctx, {OUTPUT_TYPE}`. The "
                    f"connector modules are type stubs; annotate with the types "
                    f"the tools already return instead.",
                )
            return
        if root in ALLOWED_IMPORTS:
            return
        if root in FORBIDDEN_IMPORTS:
            self.add(
                node,
                E_FORBIDDEN_IMPORT,
                f"importing `{dotted}` is forbidden",
                f"Workflows cannot touch the network, the filesystem or the OS. "
                f"Everything external happens through "
                f"`{CTX_PARAM}.<connector>.<tool>(...)`.",
            )
            return
        self.add(
            node,
            E_IMPORT_NOT_ALLOWED,
            f"`{dotted}` is not on the import allowlist",
            "Allowed imports are: "
            + ", ".join(sorted(ALLOWED_IMPORTS))
            + f" (plus `from {STUB_PACKAGE} import {CTX_TYPE}, {OUTPUT_TYPE}`). "
            f"Connectors are never imported; they arrive on `{CTX_PARAM}`.",
        )

    # -- calls -----------------------------------------------------------

    def _check_calls(self, node: ast.AST) -> None:
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            return
        name = node.func.id
        if name in FORBIDDEN_CALLS:
            self.add(
                node,
                E_FORBIDDEN_CALL,
                f"calling `{name}()` is forbidden",
                "Workflows cannot read files or run generated code. Use "
                f"`{CTX_PARAM}.<connector>.<tool>(...)` for anything external.",
            )
        elif name in DYNAMIC_ACCESS_CALLS:
            self.add(
                node,
                E_DYNAMIC_ACCESS,
                f"calling `{name}()` is forbidden",
                f"Attribute access must be static so the tools a workflow uses can "
                f"be read off the source. Write `{CTX_PARAM}.connector.tool(...)` "
                f"literally.",
            )

    # -- async -----------------------------------------------------------

    def _check_async(self, node: ast.AST) -> None:
        if isinstance(node, ast.AsyncFunctionDef):
            self.add(
                node,
                E_ASYNC,
                f"`async def {node.name}` is not supported",
                "Workflows are synchronous in v1. The runner handles the "
                "asynchronous parts; write plain `def`.",
            )
        elif isinstance(node, (ast.Await, ast.AsyncFor, ast.AsyncWith)):
            self.add(
                node,
                E_ASYNC,
                "`await` / `async for` / `async with` are not supported",
                "Workflows are synchronous in v1. Tool calls look like ordinary "
                "function calls and return their result directly.",
            )

    # -- attributes ------------------------------------------------------

    def _check_attributes(self, node: ast.AST) -> None:
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            self.add(
                node,
                E_DUNDER_ACCESS,
                f"accessing `{node.attr}` is forbidden",
                "Dunder attributes are how sandboxes get escaped. Workflows only "
                "need ordinary attribute access.",
            )

    # -- ctx -------------------------------------------------------------

    def _check_ctx_names(self, node: ast.AST) -> None:
        """`ctx` may only ever appear as `ctx.<something>`.

        That restriction is what makes stage 3 sound: if `ctx` cannot be
        rebound, aliased or passed anywhere, then every tool call in the
        program is spelled literally in the source.
        """
        if not isinstance(node, ast.Name) or node.id != CTX_PARAM:
            return

        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.add(
                node,
                E_CTX_REBOUND,
                f"`{CTX_PARAM}` cannot be reassigned",
                f"`{CTX_PARAM}` is the workflow's only handle on the outside "
                f"world; keep it exactly as `{RUN_FUNCTION}` received it.",
            )
            return

        parent = self.parents.get(node)
        if _is_dynamic_access_call(parent):
            # Already reported as `dynamic-attribute-access`; one error per
            # mistake reads better than two.
            return
        if not (isinstance(parent, ast.Attribute) and parent.value is node):
            self.add(
                node,
                E_CTX_ESCAPE,
                f"`{CTX_PARAM}` may only be used as `{CTX_PARAM}.<attribute>`",
                f"Do not pass `{CTX_PARAM}` to another function or store it in a "
                f"variable. Call tools directly: "
                f"`{CTX_PARAM}.<connector>.<tool>(...)`.",
            )
            return

        self._check_ctx_attribute(parent)

    def _check_ctx_attribute(self, connector_access: ast.Attribute) -> None:
        """`ctx.<connector>` must continue into `.<tool>(...)`."""
        if connector_access.attr == INPUTS_ATTR:
            return
        if connector_access.attr == AI_ATTR:
            self._check_ai_is_called(connector_access)
            return
        if connector_access.attr.startswith("__"):
            # `ctx.__dict__` and friends are already reported as dunder access.
            return

        tool_access = self.parents.get(connector_access)
        if not (
            isinstance(tool_access, ast.Attribute)
            and tool_access.value is connector_access
        ):
            misread = self._as_a_tool_name(connector_access.attr)
            if misread is not None:
                self.add(
                    connector_access,
                    E_UNKNOWN_CONNECTOR,
                    f"`{connector_access.attr}` is a tool, not a connector",
                    f"Write `{CTX_PARAM}.{misread}.{connector_access.attr}(...)`. "
                    f"The first attribute on `{CTX_PARAM}` is always the server, "
                    f"the second is the tool on it.",
                )
                return
            self.add(
                connector_access,
                E_CTX_TOOL_NOT_CALLED,
                f"`{CTX_PARAM}.{connector_access.attr}` must be followed by a tool "
                f"call",
                f"Write `{CTX_PARAM}.{connector_access.attr}.<tool>(...)`. A "
                f"connector cannot be stored in a variable or passed around."
                + self._connector_list(),
            )
            return

        call = self.parents.get(tool_access)
        if not (isinstance(call, ast.Call) and call.func is tool_access):
            self.add(
                tool_access,
                E_CTX_TOOL_NOT_CALLED,
                f"`{CTX_PARAM}.{connector_access.attr}.{tool_access.attr}` must be "
                f"called, not referenced",
                f"Write `{CTX_PARAM}.{connector_access.attr}.{tool_access.attr}(...)`. "
                f"Tools cannot be assigned to variables.",
            )

    def _check_ai_is_called(self, ai_access: ast.Attribute) -> None:
        """`ctx.ai` has to be called on the spot, for the same reason a tool does.

        Extraction reads AI steps off the source too -- a workflow reaching a
        model is a fact the gates need before anything runs -- and a `ctx.ai`
        handed to another function is a call site nothing can see.
        """
        call = self.parents.get(ai_access)
        if not (isinstance(call, ast.Call) and call.func is ai_access):
            self.add(
                ai_access,
                E_CTX_TOOL_NOT_CALLED,
                f"`{CTX_PARAM}.{AI_ATTR}` must be called, not referenced",
                f"Write `{CTX_PARAM}.{AI_ATTR}(system=..., user=..., "
                f"schema=...)` where you need the answer. It cannot be stored "
                f"in a variable or passed to another function.",
            )

    # -- what the connector index buys the hints -------------------------

    def _as_a_tool_name(self, attr: str) -> str | None:
        """The connector `attr` is a tool on, when it is a tool and not a server.

        `ctx.echo(...)` is the mistake a model makes after reading a tool index
        that lists tools; telling it to write `ctx.echo.<tool>(...)` sends it
        further away. Ambiguous names get no suggestion: guessing which server
        was meant would be the same mistake with better manners.
        """
        if self.connectors is None or attr in self.connectors:
            return None
        owners = [
            connector
            for connector, methods in sorted(self.connectors.items())
            if attr in methods
        ]
        return owners[0] if len(owners) == 1 else None

    def _connector_list(self) -> str:
        if not self.connectors:
            return ""
        return " Connected on this machine: " + ", ".join(sorted(self.connectors)) + "."
