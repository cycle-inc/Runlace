"""Importing MCP server definitions from the configs users already have.

We read the ``mcpServers`` blocks written by Claude Code, Cursor and project
``.mcp.json`` files, normalise them into one shape, and write the result to
``~/.runlace/config.json``. Nothing here connects to anything.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .naming import python_identifier

Transport = Literal["stdio", "http", "sse"]

CONFIG_VERSION = 1


@dataclass(frozen=True)
class Connector:
    """One MCP server, normalised."""

    name: str
    attr: str
    transport: Transport
    command: str | None = None
    args: list[str] = field(default_factory=list[str])
    env: dict[str, str] = field(default_factory=dict[str, str])
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict[str, str])

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"transport": self.transport, "attr": self.attr}
        if self.transport == "stdio":
            data["command"] = self.command
            data["args"] = self.args
            data["env"] = self.env
        else:
            data["url"] = self.url
            data["headers"] = self.headers
        return data


@dataclass
class ImportResult:
    connectors: list[Connector]
    warnings: list[str]


def default_config_sources() -> list[Path]:
    """Config files we offer to import from, most specific last."""
    return [
        Path.home() / ".claude.json",
        Path.home() / ".cursor" / "mcp.json",
        Path.cwd() / ".mcp.json",
    ]


def _server_blocks(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Every ``mcpServers`` mapping in a config document.

    ``~/.claude.json`` holds a global block plus one per project; the smaller
    formats hold exactly one.
    """
    blocks: list[dict[str, Any]] = []
    top = doc.get("mcpServers")
    if isinstance(top, dict):
        blocks.append(top)
    projects = doc.get("projects")
    if isinstance(projects, dict):
        for project in projects.values():
            if isinstance(project, dict):
                nested = project.get("mcpServers")
                if isinstance(nested, dict):
                    blocks.append(nested)
    return blocks


def _transport_of(entry: dict[str, Any]) -> Transport | None:
    declared = entry.get("type") or entry.get("transport")
    if declared in ("stdio", "http", "sse"):
        return declared
    if declared == "streamable-http":
        return "http"
    if entry.get("command"):
        return "stdio"
    if entry.get("url"):
        return "http"
    return None


def _str_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}


def parse_entry(name: str, entry: dict[str, Any]) -> tuple[Connector | None, str | None]:
    """Normalise one ``mcpServers`` entry. Returns ``(connector, warning)``."""
    attr = python_identifier(name)
    if attr is None:
        return None, f"{name}: cannot be spelled as a Python attribute, skipped"

    transport = _transport_of(entry)
    if transport is None:
        return None, f"{name}: no command or url, skipped"

    if transport == "stdio":
        command = entry.get("command")
        if not isinstance(command, str) or not command:
            return None, f"{name}: stdio server without a command, skipped"
        raw_args = entry.get("args") or []
        args = [str(a) for a in raw_args] if isinstance(raw_args, list) else []
        return Connector(
            name=name,
            attr=attr,
            transport="stdio",
            command=command,
            args=args,
            env=_str_map(entry.get("env")),
        ), None

    url = entry.get("url")
    if not isinstance(url, str) or not url:
        return None, f"{name}: {transport} server without a url, skipped"
    return Connector(
        name=name,
        attr=attr,
        transport=transport,
        url=url,
        headers=_str_map(entry.get("headers")),
    ), None


def import_from_files(sources: list[Path]) -> ImportResult:
    """Read the given config files and merge their servers.

    Later sources win on name collisions, matching the "most specific last"
    ordering of :func:`default_config_sources`.
    """
    found: dict[str, Connector] = {}
    warnings: list[str] = []

    for source in sources:
        if not source.exists():
            warnings.append(f"{source}: not found, skipped")
            continue
        try:
            doc = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(f"{source}: unreadable ({exc}), skipped")
            continue
        if not isinstance(doc, dict):
            warnings.append(f"{source}: not a JSON object, skipped")
            continue

        for block in _server_blocks(doc):
            for name, entry in block.items():
                if not isinstance(entry, dict):
                    warnings.append(f"{name}: not a JSON object, skipped")
                    continue
                connector, warning = parse_entry(str(name), entry)
                if warning:
                    warnings.append(warning)
                if connector is not None:
                    found[connector.name] = connector

    # Two different server names can collapse onto one ctx attribute
    # (``my-server`` and ``my_server``). Keep the first, warn about the rest.
    by_attr: dict[str, Connector] = {}
    connectors: list[Connector] = []
    for connector in found.values():
        clash = by_attr.get(connector.attr)
        if clash is not None:
            warnings.append(
                f"{connector.name}: collides with {clash.name} on ctx.{connector.attr}, skipped"
            )
            continue
        by_attr[connector.attr] = connector
        connectors.append(connector)

    connectors.sort(key=lambda c: c.name)
    return ImportResult(connectors=connectors, warnings=warnings)


def write_config(path: Path, connectors: list[Connector]) -> None:
    doc = {
        "version": CONFIG_VERSION,
        "connectors": {c.name: c.to_json() for c in connectors},
    }
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def read_config(path: Path) -> list[Connector]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    raw = doc.get("connectors", {})
    connectors: list[Connector] = []
    for name, entry in raw.items():
        connector, _ = parse_entry(str(name), entry)
        if connector is not None:
            connectors.append(connector)
    return connectors
