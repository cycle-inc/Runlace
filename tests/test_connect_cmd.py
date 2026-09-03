"""Adding, removing and importing connectors after `init`.

The parsing and merging here is pure, so most of these tests touch nothing.
The two that persist go through a temporary Runlace home and never connect to
anything -- discovery is somebody else's test.
"""

from __future__ import annotations

import pytest

from runlace.config import Connector
from runlace.connect_cmd import (
    LiteralSecret,
    build_connector,
    env_placeholder,
    merge,
    parse_open_webui,
    unresolved_references,
)


def connector(name: str, **kw: object) -> Connector:
    return Connector(name=name, attr=name.replace("-", "_"), transport="stdio",
                     command="npx", **kw)  # pyright: ignore[reportArgumentType]


def owui(**kw: object) -> dict[str, object]:
    """One Open WebUI tool-server entry, with the fields it always writes."""
    entry: dict[str, object] = {
        "url": "https://example.test/mcp",
        "path": "",
        "type": "mcp",
        "auth_type": "none",
        "key": "",
        "config": {"enable": True},
        "info": {"id": "example", "name": "Example"},
    }
    entry.update(kw)
    return entry


# -- merging ---


def test_adding_a_connector_keeps_the_ones_already_there() -> None:
    """The whole reason this exists: `init` would have dropped `files`."""
    result = merge([connector("files")], [connector("github")])

    assert [c.name for c in result.connectors] == ["files", "github"]
    assert result.added == ["github"]
    assert result.replaced == []


def test_adding_the_same_name_twice_replaces_rather_than_duplicates() -> None:
    result = merge([connector("files", args=["old"])], [connector("files", args=["new"])])

    assert len(result.connectors) == 1
    assert result.connectors[0].args == ["new"]
    assert result.replaced == ["files"]
    assert result.added == []


def test_a_new_connector_that_collides_on_the_ctx_attribute_is_skipped() -> None:
    """`my-server` and `my_server` are both `ctx.my_server`. First one wins."""
    result = merge([connector("my_server")], [connector("my-server")])

    assert [c.name for c in result.connectors] == ["my_server"]
    assert result.added == []
    assert "collides with my_server on ctx.my_server" in result.warnings[0]


def test_merging_nothing_changes_nothing() -> None:
    result = merge([connector("files")], [])

    assert result.changed is False
    assert [c.name for c in result.connectors] == ["files"]


# -- the Open WebUI bridge ---


def test_an_mcp_tool_server_becomes_an_http_connector() -> None:
    imported = parse_open_webui({"TOOL_SERVER_CONNECTIONS": [owui()]})

    assert len(imported.connectors) == 1
    got = imported.connectors[0]
    assert got.name == "example"
    assert got.transport == "http"
    assert got.url == "https://example.test/mcp"


def test_runlace_does_not_import_itself() -> None:
    """It is registered in the UI too, and importing it would be a loop."""
    entry = owui(info={"id": "runlace", "name": "Runlace"})
    imported = parse_open_webui({"TOOL_SERVER_CONNECTIONS": [entry]})

    assert imported.connectors == []
    assert imported.skipped == ["runlace"]


def test_an_openapi_tool_server_is_refused_rather_than_half_imported() -> None:
    """Runlace drives MCP. An OpenAPI server is not one, and saying so beats
    writing a connector that can never connect."""
    imported = parse_open_webui({"TOOL_SERVER_CONNECTIONS": [owui(type="openapi")]})

    assert imported.connectors == []
    assert "not an MCP server" in imported.warnings[0]


def test_a_bearer_token_is_imported_as_a_reference_never_as_the_token() -> None:
    """Open WebUI holds the real key. config.json gets the name of a variable."""
    entry = owui(auth_type="bearer", key="sk-the-real-secret")
    imported = parse_open_webui({"TOOL_SERVER_CONNECTIONS": [entry]})

    headers = imported.connectors[0].headers
    assert headers == {"Authorization": "Bearer ${RUNLACE_EXAMPLE_AUTHORIZATION}"}
    assert "sk-the-real-secret" not in str(imported.connectors[0])
    assert "the token stays in Open WebUI" in imported.warnings[0]


def test_a_literal_secret_header_is_replaced_with_a_reference() -> None:
    entry = owui(headers={"X-Api-Key": "literal-key", "X-Version": "2"})
    imported = parse_open_webui({"TOOL_SERVER_CONNECTIONS": [entry]})

    headers = imported.connectors[0].headers
    assert headers["X-Api-Key"] == "${RUNLACE_EXAMPLE_X_API_KEY}"
    assert headers["X-Version"] == "2", "a non-credential header is left alone"


def test_a_header_that_is_already_a_reference_is_left_alone() -> None:
    entry = owui(headers={"Authorization": "${GITHUB_TOKEN}"})
    imported = parse_open_webui({"TOOL_SERVER_CONNECTIONS": [entry]})

    assert imported.connectors[0].headers == {"Authorization": "${GITHUB_TOKEN}"}


def test_a_session_authenticated_server_says_why_it_will_not_work() -> None:
    """A browser cookie is not something a background run can replay."""
    imported = parse_open_webui({"TOOL_SERVER_CONNECTIONS": [owui(auth_type="session")]})

    assert len(imported.connectors) == 1
    assert "cannot replay" in imported.warnings[0]


def test_the_ignored_path_field_is_called_out() -> None:
    """Open WebUI ignores `path` for MCP servers; importing the join would point
    us somewhere the UI has never actually reached."""
    imported = parse_open_webui({"TOOL_SERVER_CONNECTIONS": [owui(path="mcp")]})

    assert imported.connectors[0].url == "https://example.test/mcp"
    assert "ignores it" in imported.warnings[0]


def test_a_server_with_no_usable_name_falls_back_to_its_host() -> None:
    entry = owui(info={}, url="https://tools.example.test/mcp")
    imported = parse_open_webui({"TOOL_SERVER_CONNECTIONS": [entry]})

    assert imported.connectors[0].attr == "tools_example_test"


def test_a_payload_that_is_not_what_we_expected_says_so() -> None:
    assert "not an object" in parse_open_webui("nope").warnings[0]
    assert "no TOOL_SERVER_CONNECTIONS" in parse_open_webui({}).warnings[0]


# -- adding one by hand ---


def test_a_stdio_connector_from_the_command_line() -> None:
    got = build_connector("files", command="npx", args=["-y", "server-filesystem"])

    assert got.transport == "stdio"
    assert got.command == "npx"
    assert got.args == ["-y", "server-filesystem"]


def test_an_http_connector_from_the_command_line() -> None:
    got = build_connector(
        "github",
        url="https://api.example.test/mcp",
        headers={"Authorization": "Bearer ${GITHUB_TOKEN}"},
    )

    assert got.transport == "http"
    assert got.headers == {"Authorization": "Bearer ${GITHUB_TOKEN}"}


def test_writing_a_literal_credential_header_is_refused() -> None:
    """config.json is a file on disk. `${VAR}` is resolved when the connection
    opens, so the file keeps the reference and never the secret."""
    with pytest.raises(LiteralSecret) as caught:
        build_connector("github", url="https://x.test/mcp",
                        headers={"Authorization": "Bearer ghp_realtoken"})

    assert "${RUNLACE_GITHUB_AUTHORIZATION}" in str(caught.value)


def test_writing_a_literal_credential_in_the_environment_is_refused() -> None:
    with pytest.raises(LiteralSecret) as caught:
        build_connector("github", command="npx",
                        env={"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_realtoken"})

    assert "${RUNLACE_GITHUB_GITHUB_PERSONAL_ACCESS_TOKEN}" in str(caught.value)


def test_an_ordinary_environment_variable_is_not_treated_as_a_secret() -> None:
    got = build_connector("files", command="npx", env={"NODE_ENV": "production"})

    assert got.env == {"NODE_ENV": "production"}


def test_a_connector_needs_exactly_one_of_command_or_url() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        build_connector("x")
    with pytest.raises(ValueError, match="exactly one"):
        build_connector("x", command="npx", url="https://x.test/mcp")


def test_a_name_with_nothing_usable_in_it_is_refused() -> None:
    with pytest.raises(ValueError, match="Python attribute"):
        build_connector("---", command="npx")


def test_a_name_starting_with_a_digit_is_fixed_rather_than_refused() -> None:
    """`ctx.2cool` is not legal Python; `ctx._2cool` is, and is what you get."""
    assert build_connector("2cool", command="npx").attr == "_2cool"


def test_the_placeholder_is_predictable_enough_to_put_in_an_error_message() -> None:
    assert env_placeholder("github", "X-Api-Key") == "${RUNLACE_GITHUB_X_API_KEY}"


def http(name: str, **headers: str) -> Connector:
    return Connector(name=name, attr=name, transport="http",
                     url="https://x.test/mcp", headers=dict(headers))


def test_re_importing_does_not_clobber_a_reference_that_works() -> None:
    """The UI holds the real token, so every import generates a placeholder. If
    you already pointed that header somewhere and exported it, keep yours."""
    existing = http("github", Authorization="Bearer ${GITHUB_TOKEN}")
    incoming = http("github", Authorization="Bearer ${RUNLACE_GITHUB_AUTHORIZATION}")

    result = merge([existing], [incoming])

    assert result.replaced == ["github"]
    assert result.connectors[0].headers == {"Authorization": "Bearer ${GITHUB_TOKEN}"}


def test_a_generated_placeholder_still_lands_when_there_was_nothing_before() -> None:
    result = merge([http("github")], [http("github", Authorization="${RUNLACE_GITHUB_AUTHORIZATION}")])

    assert result.connectors[0].headers == {"Authorization": "${RUNLACE_GITHUB_AUTHORIZATION}"}


def test_a_real_change_from_the_ui_does_replace() -> None:
    """Only generated placeholders defer. A header you set in the UI wins."""
    result = merge([http("github", **{"X-Version": "1"})],
                   [http("github", **{"X-Version": "2"})])

    assert result.connectors[0].headers == {"X-Version": "2"}


def test_a_reference_nobody_exported_is_named_before_the_connection_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"did not answer" is a much worse explanation than "export this"."""
    monkeypatch.delenv("RUNLACE_GITHUB_AUTHORIZATION", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_set")

    missing = unresolved_references([
        http("github", Authorization="Bearer ${RUNLACE_GITHUB_AUTHORIZATION}"),
        http("notion", Authorization="Bearer ${GITHUB_TOKEN}"),
    ])

    assert missing == {"github": ["RUNLACE_GITHUB_AUTHORIZATION"]}
