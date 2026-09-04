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


# -- `runlace model` ---


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home that exists as far as the CLI is concerned. No servers, no db."""
    monkeypatch.setenv("RUNLACE_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "config.json").write_text('{"version": 1, "connectors": {}}')
    return tmp_path / "home"


def test_setting_a_model_writes_it_and_says_what_it_means(home: Path) -> None:
    result = runner.invoke(app, ["model", "set", "qwen3:8b", "--no-check"])

    assert result.exit_code == 0, result.output
    assert '"model": "qwen3:8b"' in (home / "model.json").read_text()
    # The one consequence a user has to know about before writing a workflow.
    assert "local" in result.output


def test_a_remote_model_warns_that_ai_steps_become_side_effects(home: Path) -> None:
    result = runner.invoke(
        app,
        ["model", "set", "gpt-4o-mini", "--base-url", "https://api.openai.com/v1",
         "--api-key", "${OPENAI_API_KEY}", "--no-check"],
    )

    assert result.exit_code == 0, result.output
    assert "side effects" in result.output
    assert "park" in result.output


def test_a_literal_key_is_refused_before_it_reaches_the_disk(home: Path) -> None:
    result = runner.invoke(
        app,
        ["model", "set", "gpt-4o-mini", "--base-url", "https://api.openai.com/v1",
         "--api-key", "sk-live-abcdef", "--no-check"],
    )

    assert result.exit_code == 1
    assert not (home / "model.json").exists()
    assert "${" in result.output


def test_show_without_a_model_says_what_that_costs(home: Path) -> None:
    result = runner.invoke(app, ["model", "show"])

    assert result.exit_code == 1
    assert "ctx.ai" in result.output


def test_show_prints_the_reference_never_a_resolved_key(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-value")
    runner.invoke(
        app,
        ["model", "set", "gpt-4o-mini", "--base-url", "https://api.openai.com/v1",
         "--api-key", "${OPENAI_API_KEY}", "--no-check"],
    )

    result = runner.invoke(app, ["model", "show"])

    assert result.exit_code == 0, result.output
    assert "${OPENAI_API_KEY}" in result.output
    assert "sk-real-value" not in result.output


def test_a_model_that_does_not_answer_is_not_saved(home: Path) -> None:
    """Saving a broken backend means finding out inside a workflow instead."""
    result = runner.invoke(
        app, ["model", "set", "m", "--base-url", "http://127.0.0.1:1/v1"]
    )

    assert result.exit_code == 1
    assert not (home / "model.json").exists()
    assert "--no-check" in result.output
