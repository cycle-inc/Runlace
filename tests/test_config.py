from __future__ import annotations

import json
from pathlib import Path

from runlace.config import ImportResult, import_from_files, read_config, write_config

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
