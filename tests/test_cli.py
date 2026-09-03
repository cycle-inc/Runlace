"""The command line, at the level of what options exist and what they load.

Most of what the commands do is tested against the functions underneath them.
What is left here is the wiring, and one rule that is easy to break by adding a
command and easy to miss until it deletes something.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from runlace.cli import app

runner = CliRunner()

# Everything that connects to the servers and writes down what it found. A
# `${VAR}` is resolved by the process opening the connection, so any of these
# run without the tokens exported sees every remote server fail -- and a server
# that did not answer has its tools pruned from the database.
REDISCOVERS = ["init", "add", "import", "remove", "sync", "serve"]


@pytest.mark.parametrize("command", REDISCOVERS)
def test_every_command_that_rediscovers_takes_an_env_file(command: str) -> None:
    result = runner.invoke(app, [command, "--help"])

    assert result.exit_code == 0
    assert "--env-file" in result.output


def test_a_missing_env_file_stops_before_anything_is_pruned(tmp_path: Path) -> None:
    """Silently carrying on would be the same failure with an extra step."""
    result = runner.invoke(
        app, ["sync", "--env-file", str(tmp_path / "nope.env")]
    )

    assert result.exit_code == 1
    assert "not found" in result.output


def test_an_env_file_reports_the_names_it_loaded_and_not_the_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUNLACE_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    env = tmp_path / "tokens.env"
    env.write_text("GITHUB_TOKEN=ghp_secret\n", encoding="utf-8")

    # No home, so this stops right after loading -- which is all we are checking.
    result = runner.invoke(app, ["sync", "--env-file", str(env)])

    assert "GITHUB_TOKEN" in result.output
    assert "ghp_secret" not in result.output
