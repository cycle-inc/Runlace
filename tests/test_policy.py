"""Per-tool risk overrides (D5).

The default is pessimistic on purpose -- an unannotated tool is a side effect --
and on a server that annotates nothing that makes D6's confirm gate fire on
every run, which is the same as it not firing at all. `policy.yaml` is the
release valve, so it has to be both easy to write and hard to get silently
wrong.
"""

from __future__ import annotations

from pathlib import Path

from runlace.policy import EMPTY, read_policy, unknown_targets

YAML = """\
risk:
  github:
    search_repositories: read_only
    create_issue: side_effect
"""


def policy_file(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "policy.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_an_override_wins_over_the_annotation(tmp_path: Path) -> None:
    policy = read_policy(policy_file(tmp_path, YAML))
    assert policy.risk_for("github", "search_repositories", "side_effect") == "read_only"
    assert policy.risk_for("github", "create_issue", "read_only") == "side_effect"


def test_a_tool_with_no_override_keeps_its_classification(tmp_path: Path) -> None:
    policy = read_policy(policy_file(tmp_path, YAML))
    assert policy.risk_for("github", "untouched", "side_effect") == "side_effect"
    assert policy.risk_for("other", "search_repositories", "read_only") == "read_only"


def test_the_python_spelling_of_a_tool_is_accepted_too(tmp_path: Path) -> None:
    """The stubs show `get_annotated_message`; the server calls it `get-annotated-message`.

    A user copying from the stub should not have to discover which spelling this
    file wanted.
    """
    text = "risk:\n  demo:\n    get_annotated_message: read_only\n"
    policy = read_policy(policy_file(tmp_path, text))
    assert policy.risk_for("demo", "get-annotated-message", "side_effect") == "read_only"


# -- nothing here may take down init or a run -----------------------------


def test_a_missing_file_is_the_default_not_an_error(tmp_path: Path) -> None:
    policy = read_policy(tmp_path / "absent.yaml")
    assert policy is EMPTY
    assert policy.risk_for("github", "anything", "side_effect") == "side_effect"


def test_an_empty_file_is_the_default(tmp_path: Path) -> None:
    assert read_policy(policy_file(tmp_path, "")).is_empty()


def test_malformed_yaml_warns_and_is_ignored(tmp_path: Path) -> None:
    policy = read_policy(policy_file(tmp_path, "risk:\n  github:\n   - [unclosed\n"))
    assert policy.is_empty()
    assert any("unreadable" in w for w in policy.warnings)


def test_a_document_that_is_not_a_mapping_warns(tmp_path: Path) -> None:
    policy = read_policy(policy_file(tmp_path, "- one\n- two\n"))
    assert policy.is_empty()
    assert any("not a YAML mapping" in w for w in policy.warnings)


def test_an_unrecognised_risk_value_is_refused_not_guessed(tmp_path: Path) -> None:
    """`safe` might mean read_only, but guessing at a safety setting is not on."""
    text = "risk:\n  github:\n    a: safe\n    b: read_only\n"
    policy = read_policy(policy_file(tmp_path, text))
    assert policy.risk_for("github", "a", "side_effect") == "side_effect"
    assert policy.risk_for("github", "b", "side_effect") == "read_only"
    assert any("expected read_only or side_effect" in w for w in policy.warnings)


def test_a_connector_whose_value_is_not_a_mapping_warns(tmp_path: Path) -> None:
    policy = read_policy(policy_file(tmp_path, "risk:\n  github: read_only\n"))
    assert policy.is_empty()
    assert any("not a mapping" in w for w in policy.warnings)


def test_a_file_with_no_risk_section_is_the_default(tmp_path: Path) -> None:
    assert read_policy(policy_file(tmp_path, "something_else: 1\n")).is_empty()


# -- typos must be visible -------------------------------------------------


def test_an_override_naming_no_known_tool_is_reported(tmp_path: Path) -> None:
    """Silently ignoring it would leave the user believing a tool is gated."""
    policy = read_policy(policy_file(tmp_path, YAML))
    known = {("github", "create_issue")}
    assert unknown_targets(policy, known) == [
        "risk.github.search_repositories matches no known tool"
    ]


def test_overrides_that_all_match_report_nothing(tmp_path: Path) -> None:
    policy = read_policy(policy_file(tmp_path, YAML))
    known = {("github", "create_issue"), ("github", "search_repositories")}
    assert unknown_targets(policy, known) == []
