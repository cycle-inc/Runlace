"""Per-tool risk overrides, read from ``~/.runlace/policy.yaml`` (D5).

D5 classifies a tool from its ``readOnlyHint`` annotation and treats anything
unannotated as a side effect. That default is deliberately pessimistic, and it
has to be: a server that annotates nothing would otherwise get a free pass
through D6's confirm gate. The cost is that whole servers come out as
side-effecting when most of their tools only read, and the confirm gate stops
meaning anything if it fires on every run.

``policy.yaml`` is where the user says otherwise:

.. code-block:: yaml

    risk:
      github:
        search_repositories: read_only
        create_issue: side_effect

Tool names are the verbatim MCP ones, the same spelling ``tools/list`` returned
and the connector index shows -- not the Python method name, which can differ
(``get-annotated-message`` becomes ``get_annotated_message``). Both spellings
are accepted, because a user reading a stub should not have to know which one
this file wants.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .naming import python_identifier
from .risk import READ_ONLY, SIDE_EFFECT, Risk

RISK_VALUES: dict[str, Risk] = {READ_ONLY: READ_ONLY, SIDE_EFFECT: SIDE_EFFECT}


@dataclass(frozen=True)
class Policy:
    """Risk overrides, keyed by connector and then by tool."""

    risk: dict[str, dict[str, Risk]] = field(
        default_factory=dict[str, dict[str, Risk]]
    )
    warnings: list[str] = field(default_factory=list[str])

    def risk_for(self, connector: str, tool: str, default: Risk) -> Risk:
        """The effective risk of one tool: the override if there is one."""
        overrides = self.risk.get(connector)
        if not overrides:
            return default
        if tool in overrides:
            return overrides[tool]
        # The stubs spell tools as Python methods, so that is what a user is
        # most likely to copy into this file.
        method = python_identifier(tool)
        if method is not None and method in overrides:
            return overrides[method]
        return default

    def is_empty(self) -> bool:
        return not self.risk


EMPTY = Policy()


def read_policy(path: Path) -> Policy:
    """Load ``policy.yaml``. A missing file is not an error -- it is the default.

    Nothing here raises. A policy file that is unreadable, malformed or full of
    values that are not risks would otherwise take down `runlace init` and every
    run with it, and the failure mode of "your override was ignored" is easier
    to recover from than "nothing works". Every problem becomes a warning the
    caller can print.
    """
    if not path.exists():
        return EMPTY

    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return Policy(warnings=[f"{path.name}: unreadable ({_short(exc)}), ignored"])

    if document is None:
        return EMPTY
    if not isinstance(document, dict):
        return Policy(warnings=[f"{path.name}: not a YAML mapping, ignored"])

    section = document.get("risk")
    if section is None:
        return EMPTY
    if not isinstance(section, dict):
        return Policy(warnings=[f"{path.name}: `risk` is not a mapping, ignored"])

    risk: dict[str, dict[str, Risk]] = {}
    warnings: list[str] = []
    for connector, tools in section.items():
        if not isinstance(tools, dict):
            warnings.append(f"{path.name}: risk.{connector} is not a mapping, ignored")
            continue
        for tool, value in tools.items():
            resolved = RISK_VALUES.get(str(value))
            if resolved is None:
                warnings.append(
                    f"{path.name}: risk.{connector}.{tool} is {value!r}, "
                    f"expected {READ_ONLY} or {SIDE_EFFECT}, ignored"
                )
                continue
            risk.setdefault(str(connector), {})[str(tool)] = resolved

    return Policy(risk=risk, warnings=warnings)


def unknown_targets(policy: Policy, known: set[tuple[str, str]]) -> list[str]:
    """Overrides naming a connector or tool this machine does not have.

    A typo in this file fails silently and dangerously: you believe a tool is
    gated and it is not. ``known`` holds ``(connector, tool)`` in both the
    verbatim and the Python spelling.
    """
    missing: list[str] = []
    for connector, tools in sorted(policy.risk.items()):
        for tool in sorted(tools):
            if (connector, tool) not in known:
                missing.append(f"risk.{connector}.{tool} matches no known tool")
    return missing


def _short(exc: Exception) -> str:
    return " ".join(str(exc).split())[:120]
