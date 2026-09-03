from __future__ import annotations

from pathlib import Path

from runlace.typecheck import check_stubs


def make_types(root: Path, ctx_body: str) -> Path:
    types = root / "runlace_types"
    (types / "connectors").mkdir(parents=True)
    (types / "__init__.pyi").write_text("from .ctx import Ctx as Ctx\n", encoding="utf-8")
    (types / "ctx.pyi").write_text(ctx_body, encoding="utf-8")
    return types


def test_clean_stubs_pass(tmp_path: Path) -> None:
    types = make_types(tmp_path, "class Ctx:\n    inputs: dict[str, object]\n")
    result = check_stubs(types)
    assert result.ok, result.report()


def test_pyright_actually_analyses_the_files(tmp_path: Path) -> None:
    # Guards the failure mode where pyright is handed a path it ignores and
    # reports success without checking anything.
    types = make_types(tmp_path, "class Ctx:\n    inputs: dict[str, object]\n")
    assert check_stubs(types).files_analyzed >= 2


def test_strict_violation_is_reported_with_a_line_number(tmp_path: Path) -> None:
    types = make_types(tmp_path, "class Ctx:\n    inputs: dict\n")
    result = check_stubs(types)
    assert not result.ok
    assert result.errors[0].line == 2
    assert result.errors[0].rule == "reportMissingTypeArgument"


def test_syntax_error_in_a_stub_is_reported(tmp_path: Path) -> None:
    types = make_types(tmp_path, "class Ctx:\n    inputs: dict[str, object]\n")
    (types / "connectors" / "bad.pyi").write_text("class Broken(\n", encoding="utf-8")
    assert not check_stubs(types).ok


def test_missing_directory_is_an_error_not_a_pass(tmp_path: Path) -> None:
    result = check_stubs(tmp_path / "absent")
    assert not result.ok


def test_config_file_is_cleaned_up(tmp_path: Path) -> None:
    types = make_types(tmp_path, "class Ctx:\n    inputs: dict[str, object]\n")
    check_stubs(types)
    assert list(tmp_path.glob(".runlace-pyright-*")) == []
