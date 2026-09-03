from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from runlace.config import (
    Connector,
    ImportResult,
    MissingEnvVars,
    import_from_files,
    load_env_file,
    read_config,
    write_config,
)

from .conftest import FIXTURES


def names(result: ImportResult) -> list[str]:
    return [c.name for c in result.connectors]


def test_imports_global_and_project_blocks_from_claude_config() -> None:
    result = import_from_files([FIXTURES / "claude_config.json"])
    assert names(result) == ["global-stdio", "project-sse", "remote-api"]


def test_normalises_each_transport() -> None:
    result = import_from_files([FIXTURES / "claude_config.json"])
    by_name = {c.name: c for c in result.connectors}

    stdio = by_name["global-stdio"]
    assert (stdio.transport, stdio.command, stdio.args) == ("stdio", "node", ["server.js"])
    assert stdio.env == {"TOKEN": "abc"}

    http = by_name["remote-api"]
    assert http.transport == "http"
    assert http.headers == {"Authorization": "Bearer xyz"}

    assert by_name["project-sse"].transport == "sse"


def test_server_name_becomes_a_ctx_attribute() -> None:
    result = import_from_files([FIXTURES / "claude_config.json"])
    assert {c.name: c.attr for c in result.connectors}["global-stdio"] == "global_stdio"


def test_entry_without_command_or_url_is_skipped_with_a_warning() -> None:
    result = import_from_files([FIXTURES / "claude_config.json"])
    assert "broken-no-command" not in names(result)
    assert any("broken-no-command" in w for w in result.warnings)


def test_missing_file_warns_but_does_not_fail(tmp_path: Path) -> None:
    result = import_from_files([tmp_path / "nope.json"])
    assert result.connectors == []
    assert any("not found" in w for w in result.warnings)


def test_malformed_json_warns_but_does_not_fail(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    result = import_from_files([bad])
    assert result.connectors == []
    assert any("unreadable" in w for w in result.warnings)


def test_later_source_wins_on_name_collision(tmp_path: Path) -> None:
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    first.write_text(json.dumps({"mcpServers": {"s": {"command": "one"}}}), encoding="utf-8")
    second.write_text(json.dumps({"mcpServers": {"s": {"command": "two"}}}), encoding="utf-8")
    result = import_from_files([first, second])
    assert [c.command for c in result.connectors] == ["two"]


def test_names_colliding_on_one_ctx_attribute_are_rejected(tmp_path: Path) -> None:
    config = tmp_path / "c.json"
    config.write_text(
        json.dumps({"mcpServers": {"my-server": {"command": "a"}, "my_server": {"command": "b"}}}),
        encoding="utf-8",
    )
    result = import_from_files([config])
    assert len(result.connectors) == 1
    assert any("collides" in w for w in result.warnings)


def test_config_round_trips(tmp_path: Path) -> None:
    imported = import_from_files([FIXTURES / "claude_config.json"])
    target = tmp_path / "config.json"
    write_config(target, imported.connectors)
    assert read_config(target) == imported.connectors


# -- ${VAR} expansion ------------------------------------------------------
#
# D8 allows remote servers with static header auth, so config.json would
# otherwise hold a bearer token in plaintext. The reference is what gets stored;
# the value is only ever read from the environment, when a connection opens.


def http_connector(**overrides: object) -> Connector:
    defaults: dict[str, object] = {
        "name": "github",
        "attr": "github",
        "transport": "http",
        "url": "https://api.githubcopilot.com/mcp/",
        "headers": {"Authorization": "Bearer ${GITHUB_PAT}"},
    }
    return Connector(**{**defaults, **overrides})  # type: ignore[arg-type]


def test_a_header_reference_is_resolved_from_the_environment() -> None:
    resolved = http_connector().resolved({"GITHUB_PAT": "ghp_secret"})
    assert resolved.headers == {"Authorization": "Bearer ghp_secret"}


def test_the_stored_config_keeps_the_reference_not_the_secret(tmp_path: Path) -> None:
    """The whole point: the file on disk must never contain the token."""
    target = tmp_path / "config.json"
    write_config(target, [http_connector()])
    text = target.read_text(encoding="utf-8")
    assert "${GITHUB_PAT}" in text
    assert "ghp_secret" not in text
    assert read_config(target) == [http_connector()]


def test_a_reference_with_nothing_behind_it_is_an_error() -> None:
    """Better than sending `Bearer ${GITHUB_PAT}` and collecting a puzzling 401."""
    with pytest.raises(MissingEnvVars) as raised:
        http_connector().resolved({})
    assert raised.value.names == ["GITHUB_PAT"]
    assert "GITHUB_PAT" in str(raised.value)


def test_every_missing_variable_is_reported_at_once() -> None:
    connector = http_connector(
        url="https://${HOST}/mcp/",
        headers={"Authorization": "Bearer ${GITHUB_PAT}", "X-Org": "${ORG}"},
    )
    with pytest.raises(MissingEnvVars) as raised:
        connector.resolved({"ORG": "cycle-inc"})
    assert raised.value.names == ["HOST", "GITHUB_PAT"]


def test_stdio_env_and_args_are_resolved_too() -> None:
    connector = Connector(
        name="local",
        attr="local",
        transport="stdio",
        command="server",
        args=["--project", "${PROJECT}"],
        env={"TOKEN": "${TOKEN}"},
    )
    resolved = connector.resolved({"PROJECT": "vega", "TOKEN": "t"})
    assert resolved.args == ["--project", "vega"]
    assert resolved.env == {"TOKEN": "t"}


def test_the_command_itself_is_left_alone() -> None:
    """It is a binary looked up on PATH; substituting there would be a surprise."""
    connector = Connector(
        name="local", attr="local", transport="stdio", command="${EVIL}", args=[]
    )
    assert connector.resolved({}).command == "${EVIL}"


def test_a_bare_dollar_name_is_not_a_reference() -> None:
    """Only ${VAR}. A bare $VAR is too easy to write by accident in a URL."""
    connector = http_connector(headers={"X-Cost": "$USD and $100"})
    assert connector.resolved({"USD": "no"}).headers == {"X-Cost": "$USD and $100"}


def test_a_connector_without_references_is_unchanged() -> None:
    connector = http_connector(headers={"Authorization": "Bearer literal"})
    assert connector.resolved({}) == connector


# -- --env-file ---


def test_an_env_file_puts_its_names_in_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Why this exists: `runlace serve` resolves ${VAR} from its own
    environment, not from the shell where you ran `runlace add`."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    path = tmp_path / ".env"
    path.write_text("# a comment\n\nexport GITHUB_TOKEN=\"ghp_x\"\nNOTION_KEY=secret\n")

    assert sorted(load_env_file(path)) == ["GITHUB_TOKEN", "NOTION_KEY"]
    assert os.environ["GITHUB_TOKEN"] == "ghp_x"
    assert os.environ["NOTION_KEY"] == "secret"


def test_the_environment_wins_over_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file is a fallback. Overriding what you just exported would be rude."""
    monkeypatch.setenv("GITHUB_TOKEN", "from-the-shell")
    path = tmp_path / ".env"
    path.write_text("GITHUB_TOKEN=from-the-file\n")

    assert load_env_file(path) == []
    assert os.environ["GITHUB_TOKEN"] == "from-the-shell"


def test_a_line_that_is_not_an_assignment_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OK", raising=False)
    path = tmp_path / ".env"
    path.write_text("just some words\nnot-an-identifier=1\nOK=1\n")

    assert load_env_file(path) == ["OK"]
