"""Running pyright as a subprocess (D9).

M1 uses this for one thing only: proving the generated stubs are clean under
strict mode. From M2 the same runner checks workflow files against them.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

PYRIGHT_TIMEOUT_SECONDS = 300


@dataclass
class Diagnostic:
    file: str
    line: int
    message: str
    rule: str | None

    def __str__(self) -> str:
        suffix = f" ({self.rule})" if self.rule else ""
        return f"{self.file}:{self.line}: {self.message}{suffix}"


@dataclass
class TypecheckResult:
    ok: bool
    errors: list[Diagnostic]
    summary: str
    files_analyzed: int = 0

    def report(self) -> str:
        if self.ok:
            return self.summary
        return "\n".join([self.summary, *(f"  {e}" for e in self.errors)])


def _pyright_command() -> list[str]:
    executable = shutil.which("pyright")
    if executable:
        return [executable]
    return [sys.executable, "-m", "pyright"]


def _run(config: dict[str, object], config_path: Path) -> TypecheckResult:
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    command = [
        *_pyright_command(),
        "--project",
        str(config_path),
        "--pythonpath",
        sys.executable,
        "--outputjson",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=PYRIGHT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return TypecheckResult(False, [], "pyright is not installed (pip install pyright)")
    except subprocess.TimeoutExpired:
        return TypecheckResult(False, [], f"pyright timed out after {PYRIGHT_TIMEOUT_SECONDS}s")

    return _parse(completed.stdout, completed.stderr)


def _parse(stdout: str, stderr: str) -> TypecheckResult:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        detail = (stderr or stdout).strip() or "no output"
        return TypecheckResult(False, [], f"could not read pyright output: {detail[:500]}")

    errors = [
        Diagnostic(
            file=str(d.get("file", "?")),
            line=int(d.get("range", {}).get("start", {}).get("line", 0)) + 1,
            message=str(d.get("message", "")).replace("\n", " "),
            rule=d.get("rule"),
        )
        for d in payload.get("generalDiagnostics", [])
        if d.get("severity") == "error"
    ]
    counts = payload.get("summary", {})
    files_analyzed = int(counts.get("filesAnalyzed", 0))
    summary = (
        f"pyright: {counts.get('errorCount', len(errors))} error(s) "
        f"in {files_analyzed} file(s)"
    )
    return TypecheckResult(
        ok=not errors, errors=errors, summary=summary, files_analyzed=files_analyzed
    )


def check_paths(root: Path, targets: list[Path], *, types_dir: Path) -> TypecheckResult:
    """Typecheck ``targets`` in strict mode, with ``types_dir`` importable.

    pyright silently analyses nothing when ``include`` points outside the
    project root, so the config has to live next to the code it checks. We
    write it under ``root`` and take it away again.
    """
    missing = [t for t in targets if not t.exists()]
    if missing:
        return TypecheckResult(False, [], f"does not exist: {missing[0]}")

    handle, name = tempfile.mkstemp(prefix=".runlace-pyright-", suffix=".json", dir=root)
    os.close(handle)
    config_path = Path(name)
    try:
        result = _run(
            {
                "include": [_relative(t, root) for t in targets],
                # "." makes `from runlace_types import Ctx` resolve, because
                # runlace_types/ sits directly under the Runlace home.
                "extraPaths": [_relative(types_dir.parent, root)],
                "typeCheckingMode": "strict",
                "pythonVersion": "3.11",
                "reportMissingModuleSource": "none",
            },
            config_path,
        )
    finally:
        config_path.unlink(missing_ok=True)

    if result.ok and result.files_analyzed == 0:
        return TypecheckResult(
            False, [], "pyright analysed no files -- nothing was verified"
        )
    return result


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())) or "."
    except ValueError:
        return str(path)


def check_stubs(types_dir: Path) -> TypecheckResult:
    """Typecheck ``runlace_types/`` in strict mode."""
    if not types_dir.exists():
        return TypecheckResult(False, [], f"{types_dir} does not exist")
    return check_paths(types_dir.parent, [types_dir], types_dir=types_dir)
