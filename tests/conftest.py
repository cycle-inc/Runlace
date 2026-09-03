from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from runlace.paths import RunlacePaths

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RunlacePaths:
    """An isolated Runlace home, so tests never touch the real ~/.runlace."""
    home = tmp_path / "runlace-home"
    monkeypatch.setenv("RUNLACE_HOME", str(home))
    p = RunlacePaths(home)
    p.create()
    return p


@pytest.fixture(scope="session")
def npx() -> str:
    executable = shutil.which("npx")
    if executable is None:
        pytest.skip("npx is not on PATH")
    return executable
