"""Adding and removing connectors once ``runlace init`` has run.

``init`` imports a list of configs and writes exactly that list. That is right
the first time and wrong every time after: adding one server should not silently
drop the others. Everything here **merges** instead, and re-discovers the whole
merged list, because that is also what the database expects -- a connector
missing from the list is a connector pruned.

``import`` exists because of an asymmetry worth stating plainly. A chat UI like
Open WebUI can only register MCP servers it can reach over HTTP; it has no way
to launch ``npx``. Runlace speaks stdio *and* HTTP, so everything the UI knows
about, Runlace can drive -- but not the reverse. The bridge only runs one way,
and this is that direction.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlparse

import httpx2

from .config import Connector, Transport, read_config, write_config
from .discovery import DEFAULT_TIMEOUT_SECONDS
from .init_cmd import InitReport, discover_and_persist
from .naming import python_identifier
from .paths import RunlacePaths

# Header names whose value is a credential often enough that we refuse to write
# a literal one down. `${VAR}` is resolved when the connection opens, so the
# config file keeps the reference and never the secret.
SECRET_HEADERS = frozenset(
    {"authorization", "x-api-key", "api-key", "x-auth-token", "x-api-token", "cookie"}
)

# Same idea for the environment of a stdio server, where the convention is a
# name rather than a fixed list: GITHUB_PERSONAL_ACCESS_TOKEN, NOTION_API_KEY.
_SECRET_ENV = re.compile(r"TOKEN|KEY|SECRET|PASSWORD|CREDENTIAL", re.IGNORECASE)

# A credential header is almost never *only* the reference: the normal spelling
# is `Bearer ${GITHUB_TOKEN}`. So the test is whether the value uses the
# mechanism at all, not whether it is nothing else.
_ENV_REF = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")


class LiteralSecret(Exception):
    """A credential was about to be written to config.json in plaintext."""

    def __init__(self, where: str, suggestion: str) -> None:
        self.where = where
        self.suggestion = suggestion
        super().__init__(
            f"{where} looks like a credential, and config.json is a file on disk. "
            f"Use {suggestion} instead and export it before the next run."
        )


def env_placeholder(connector: str, label: str) -> str:
    """The ``${VAR}`` reference we suggest for a given connector and header."""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", f"{connector}_{label}").strip("_").upper()
    return "${RUNLACE_" + slug + "}"


def _is_reference(value: str) -> bool:
    return bool(_ENV_REF.search(value))


def check_headers(connector: str, headers: dict[str, str]) -> None:
    """Raise if a credential header carries a literal instead of a reference."""
    for name, value in headers.items():
        if name.lower() in SECRET_HEADERS and not _is_reference(value):
            raise LiteralSecret(
                f"header `{name}`", f'"{env_placeholder(connector, name)}"'
            )


def check_env(connector: str, env: dict[str, str]) -> None:
    """Raise if an environment entry that reads like a credential is a literal."""
    for name, value in env.items():
        if _SECRET_ENV.search(name) and not _is_reference(value):
            raise LiteralSecret(
                f"environment variable `{name}`",
                f'"{env_placeholder(connector, name)}"',
            )


# -- merging ---


@dataclass
class MergeResult:
    """The full connector list to persist, and what changed to get there."""

    connectors: list[Connector] = field(default_factory=list[Connector])
    added: list[str] = field(default_factory=list[str])
    replaced: list[str] = field(default_factory=list[str])
    removed: list[str] = field(default_factory=list[str])
    warnings: list[str] = field(default_factory=list[str])

    @property
    def changed(self) -> bool:
        return bool(self.added or self.replaced or self.removed)


def _keep_working_references(previous: Connector, incoming: Connector) -> Connector:
    """Do not let a generated placeholder overwrite a reference that works.

    Re-importing from a chat UI produces ``${RUNLACE_<NAME>_AUTHORIZATION}`` for
    every credential header, because the UI holds the real token and we refuse
    to copy it. If you had already pointed that header at ``${GITHUB_TOKEN}``
    and exported it, clobbering it would break a connector that was fine.
    Anything you typed yourself still wins over anything we generated.
    """
    kept = dict(incoming.headers)
    for name, value in incoming.headers.items():
        old = previous.headers.get(name)
        if old and "${RUNLACE_" in value and "${RUNLACE_" not in old:
            kept[name] = old
    return replace(incoming, headers=kept)


def merge(existing: list[Connector], incoming: list[Connector]) -> MergeResult:
    """Fold ``incoming`` into ``existing``. Same name means replace, not duplicate.

    Pure: reads nothing and writes nothing. The caller decides whether the
    result is worth persisting.
    """
    result = MergeResult()
    by_name = {c.name: c for c in existing}

    for connector in incoming:
        clash = next(
            (
                c
                for c in by_name.values()
                if c.attr == connector.attr and c.name != connector.name
            ),
            None,
        )
        if clash is not None:
            result.warnings.append(
                f"{connector.name}: collides with {clash.name} on "
                f"ctx.{connector.attr}, skipped"
            )
            continue
        previous = by_name.get(connector.name)
        if previous is not None:
            connector = _keep_working_references(previous, connector)
            result.replaced.append(connector.name)
        else:
            result.added.append(connector.name)
        by_name[connector.name] = connector

    result.connectors = sorted(by_name.values(), key=lambda c: c.name)
    return result


def unresolved_references(connectors: list[Connector]) -> dict[str, list[str]]:
    """Variables each connector refers to that are not set right now.

    Worth saying out loud before discovery rather than after: a connector whose
    token is missing fails to connect, and "did not answer" is a much worse
    explanation than "export RUNLACE_GITHUB_AUTHORIZATION".
    """
    missing: dict[str, list[str]] = {}
    for connector in connectors:
        values = [
            *connector.args,
            *connector.env.values(),
            *connector.headers.values(),
            connector.url or "",
        ]
        names = {
            name
            for value in values
            for name in _ENV_REF.findall(value)
            if os.environ.get(name.removeprefix("${").removesuffix("}")) is None
        }
        unset = sorted(n.removeprefix("${").removesuffix("}") for n in names)
        if unset:
            missing[connector.name] = unset
    return missing


def _stored(paths: RunlacePaths) -> list[Connector]:
    """The connectors on disk, or none. A home without a config is an empty one."""
    if not paths.config.exists():
        return []
    return read_config(paths.config)


def _persist(
    paths: RunlacePaths, connectors: list[Connector], *, timeout: float
) -> InitReport:
    """Write the list, then re-discover all of it.

    Re-discovering servers that did not change is the boring choice and costs a
    few seconds. It is also the correct one: the database prunes any connector
    absent from the list it is handed, so a partial list would delete tools.
    """
    paths.create()
    write_config(paths.config, connectors)
    report = InitReport(home=paths.home)
    discover_and_persist(paths, connectors, report, timeout=timeout)
    return report


def add_connectors(
    paths: RunlacePaths,
    incoming: list[Connector],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[MergeResult, InitReport]:
    """Merge ``incoming`` into the stored config and re-discover everything."""
    merged = merge(_stored(paths), incoming)
    return merged, _persist(paths, merged.connectors, timeout=timeout)


def remove_connector(
    paths: RunlacePaths,
    name: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[MergeResult, InitReport | None]:
    """Drop one connector by name. Returns ``(result, None)`` if it was absent.

    Workflows compiled against its tools are left alone: they stay readable, and
    the next run is refused rather than half-executed. That is D1 doing its job,
    not an oversight.
    """
    existing = _stored(paths)
    kept = [c for c in existing if c.name != name]
    result = MergeResult(connectors=kept)
    if len(kept) == len(existing):
        result.warnings.append(f"no connector called `{name}`")
        return result, None
    result.removed.append(name)
    return result, _persist(paths, kept, timeout=timeout)


# -- the Open WebUI bridge ---


@dataclass
class Imported:
    """What one chat UI's tool-server list looks like once normalised."""

    connectors: list[Connector] = field(default_factory=list[Connector])
    warnings: list[str] = field(default_factory=list[str])
    skipped: list[str] = field(default_factory=list[str])


def fetch_open_webui(base_url: str, token: str, *, timeout: float = 10.0) -> Any:
    """GET the tool-server list. Needs an Open WebUI **admin** token."""
    url = base_url.rstrip("/") + "/api/v1/configs/tool_servers"
    response = httpx2.get(
        url, headers={"Authorization": f"Bearer {token}"}, timeout=timeout
    )
    response.raise_for_status()
    return response.json()


def _connector_name(entry: dict[str, Any], url: str) -> str | None:
    info = entry.get("info")
    candidates: list[str] = []
    if isinstance(info, dict):
        for key in ("id", "name"):
            value = info.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(value)
    host = urlparse(url).hostname
    if host:
        candidates.append(host)
    for candidate in candidates:
        if python_identifier(candidate) is not None:
            return candidate
    return None


def parse_open_webui(payload: Any, *, skip: str = "runlace") -> Imported:
    """Normalise Open WebUI's tool-server list into connectors. Pure.

    Only ``type: "mcp"`` entries come across. An OpenAPI tool server is not an
    MCP server -- Runlace has nothing to drive it with, and silently importing a
    broken connector is worse than saying so.

    Credentials never come across either. Open WebUI holds the real token; we
    write a ``${VAR}`` reference next to it and say which one to export.
    """
    result = Imported()
    if not isinstance(payload, dict):
        result.warnings.append("Open WebUI returned something that is not an object")
        return result
    entries = payload.get("TOOL_SERVER_CONNECTIONS")
    if not isinstance(entries, list):
        result.warnings.append("Open WebUI returned no TOOL_SERVER_CONNECTIONS list")
        return result

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        url = entry.get("url")
        if not isinstance(url, str) or not url:
            result.warnings.append("a tool server has no url, skipped")
            continue

        kind = entry.get("type") or "openapi"
        if kind != "mcp":
            result.warnings.append(
                f"{url}: type `{kind}`, not an MCP server -- skipped"
            )
            continue

        name = _connector_name(entry, url)
        if name is None:
            result.warnings.append(f"{url}: cannot be named, skipped")
            continue
        if name.lower() == skip.lower():
            result.skipped.append(name)
            continue

        # Open WebUI ignores `path` for MCP servers -- it connects to `url` and
        # nothing else. Importing the join would point us somewhere the UI has
        # never actually reached.
        path = entry.get("path")
        if isinstance(path, str) and path.strip():
            result.warnings.append(
                f"{name}: `path` is set to `{path}` but Open WebUI ignores it for "
                f"MCP servers, so it is ignored here too"
            )

        headers, notes = _headers_for(name, entry)
        result.warnings.extend(notes)

        attr = python_identifier(name)
        assert attr is not None  # _connector_name only returns nameable candidates
        result.connectors.append(
            Connector(
                name=name,
                attr=attr,
                transport="http",
                url=url,
                headers=headers,
            )
        )

    result.connectors.sort(key=lambda c: c.name)
    return result


def _headers_for(name: str, entry: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    headers: dict[str, str] = {}
    notes: list[str] = []

    raw = entry.get("headers")
    if isinstance(raw, dict):
        for key, value in raw.items():
            key, value = str(key), str(value)
            if key.lower() in SECRET_HEADERS and not _is_reference(value):
                headers[key] = env_placeholder(name, key)
                notes.append(
                    f"{name}: header `{key}` held a literal value, replaced with "
                    f"{env_placeholder(name, key)}"
                )
            else:
                headers[key] = value

    auth = entry.get("auth_type") or "none"
    if auth == "bearer" and "authorization" not in {k.lower() for k in headers}:
        placeholder = env_placeholder(name, "Authorization")
        headers["Authorization"] = f"Bearer {placeholder}"
        notes.append(
            f"{name}: uses bearer auth, and the token stays in Open WebUI -- "
            f"{placeholder} stands in for it"
        )
    elif auth in ("session", "oauth_2.1", "oauth_2.1_static", "system_oauth"):
        notes.append(
            f"{name}: authenticates with `{auth}`, which is a browser session "
            f"Runlace cannot replay. Add a token header by hand if the server "
            f"accepts one."
        )

    return headers, notes


# -- adding one by hand ---


def build_connector(
    name: str,
    *,
    command: str | None = None,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    url: str | None = None,
    headers: dict[str, str] | None = None,
    transport: Transport | None = None,
) -> Connector:
    """One connector from CLI arguments, refusing to write a secret down.

    Raises ``ValueError`` for a shape that makes no sense and ``LiteralSecret``
    for a credential that should have been a ``${VAR}``.
    """
    attr = python_identifier(name)
    if attr is None:
        raise ValueError(f"`{name}` cannot be spelled as a Python attribute")
    if bool(command) == bool(url):
        raise ValueError("give exactly one of --command (stdio) or --url (http, sse)")

    if command:
        env = env or {}
        check_env(name, env)
        return Connector(
            name=name,
            attr=attr,
            transport="stdio",
            command=command,
            args=list(args or []),
            env=env,
        )

    assert url is not None
    headers = headers or {}
    check_headers(name, headers)
    if transport not in ("http", "sse"):
        transport = "http"
    return Connector(
        name=name, attr=attr, transport=transport, url=url, headers=headers
    )


# -- adding one from the chat ---

# `runlace add` can launch a local process; this cannot, and the difference is
# deliberate. A command in config.json is "run this on my machine every time a
# workflow touches it", and the value would be arriving from a model that may
# have read it off a web page a moment earlier. A URL only reaches outwards.
# Anyone who genuinely wants a stdio server can type it in their own shell.
_REMOTE_ONLY = (
    "This tool only adds servers reachable over HTTP. For a local one launched "
    "by a command, run `runlace add <name> --command ... --arg ...` in a shell."
)


def add_connector(
    paths: RunlacePaths,
    *,
    name: str,
    url: str,
    headers: dict[str, str] | None = None,
    transport: str = "http",
    confirm: bool = False,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Add one HTTP connector, behind a confirmation gate. Returns JSON.

    The body of the ``add_connector`` MCP tool, kept here so it can be tested
    without a server and so it shares the merge, the secret check and the
    re-discovery with the CLI rather than reimplementing them.

    Nothing is written unless ``confirm`` is true.
    """
    if transport not in ("http", "sse"):
        return {
            "ok": False,
            "code": "bad-transport",
            "error": f"`{transport}` is not a transport this tool can add",
            "hint": f"Use `http`, or `sse` for an older server. {_REMOTE_ONLY}",
        }

    try:
        connector = build_connector(
            name, url=url, headers=headers or {}, transport=transport
        )
    except LiteralSecret as exc:
        reference = exc.suggestion.strip('"')
        variable = reference.removeprefix("${").removesuffix("}")
        return {
            "ok": False,
            "code": "literal-secret",
            "error": str(exc),
            "hint": (
                f"Ask the human to run `export {variable}=<token>` in the shell "
                f"that starts Runlace, then call this again with `{reference}` as "
                "the header value. Do not ask them to paste the token to you: it "
                "does not need to pass through this conversation."
            ),
        }
    except ValueError as exc:
        return {"ok": False, "code": "bad-name", "error": str(exc)}

    planned = merge(_stored(paths), [connector])
    if not planned.changed:
        return {
            "ok": False,
            "code": "name-collision",
            "error": planned.warnings[0] if planned.warnings else "nothing to add",
            "hint": "Pick a different name for this connector.",
        }

    needs_env = unresolved_references([connector]).get(name, [])
    action = "replace" if planned.replaced else "add"

    if not confirm:
        return {
            "ok": False,
            "code": "needs-confirmation",
            "action": action,
            "connector": {
                "name": name,
                "attr": connector.attr,
                "transport": transport,
                "url": url,
                "headers": sorted(connector.headers),
            },
            "needs_env": needs_env,
            "hint": (
                "Show the human this URL and ask whether to add it. Adding a "
                "connector widens what every future workflow on this machine can "
                "reach. Call again with confirm=true once they agree."
                + (
                    ""
                    if not needs_env
                    else " They must also export "
                    + ", ".join(needs_env)
                    + " and restart `runlace serve`, which reads the environment "
                    "once at startup."
                )
            ),
        }

    merged, report = add_connectors(paths, [connector], timeout=timeout)
    row = next((r for r in report.rows if r.name == name), None)
    warnings = [*merged.warnings, *report.warnings]

    if row is None or not row.status.startswith("connected"):
        return {
            "ok": False,
            "code": "connector-unreachable",
            "connector": name,
            "status": row.status if row is not None else "not discovered",
            "error": f"{name} is written to config.json but did not answer",
            "needs_env": needs_env,
            "warnings": warnings,
            "hint": (
                "Check the url and the credential. Fix it by calling this tool "
                "again with the same name -- it replaces rather than duplicates."
                + (
                    ""
                    if not needs_env
                    else " " + ", ".join(needs_env) + " is not set in this process."
                )
            ),
        }

    return {
        "ok": True,
        "connector": name,
        "attr": connector.attr,
        "action": "replaced" if merged.replaced else "added",
        "tools": row.tools,
        "warnings": warnings,
        "next": (
            f"Call get_skill again: the stubs were regenerated and "
            f"ctx.{connector.attr} now exists, with its tools and their risk."
        ),
    }
