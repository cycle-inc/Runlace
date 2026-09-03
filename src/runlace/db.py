"""SQLite storage.

The four workflow tables come straight from the spec and are created here at
bootstrap even though M1 does not write to them. ``connectors`` and ``tools``
are M1's own: discovery has to persist somewhere, and D10 puts everything but
workflow code in the database.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .hashing import canonical_json

Connection = sqlite3.Connection

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Discovery (M1)

CREATE TABLE IF NOT EXISTS connectors (
    name          TEXT PRIMARY KEY,          -- verbatim server name from the config
    attr          TEXT NOT NULL UNIQUE,      -- how it is spelled as ctx.<attr>
    transport     TEXT NOT NULL,             -- stdio | http | sse
    config_json   TEXT NOT NULL,
    status        TEXT NOT NULL,             -- connected | skipped | error
    detail        TEXT,
    tool_count    INTEGER NOT NULL DEFAULT 0,
    discovered_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tools (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    connector          TEXT NOT NULL REFERENCES connectors(name) ON DELETE CASCADE,
    name               TEXT NOT NULL,        -- verbatim MCP tool name (D2)
    method             TEXT NOT NULL,        -- how it is spelled in the stubs
    description        TEXT,
    input_schema_json  TEXT NOT NULL,
    output_schema_json TEXT,
    annotations_json   TEXT,
    risk               TEXT NOT NULL,        -- read_only | side_effect (D5)
    schema_hash        TEXT NOT NULL,
    UNIQUE(connector, name)
);

-- Workflows (populated from M2 onwards)

CREATE TABLE IF NOT EXISTS workflows (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,
    description    TEXT,
    latest_version TEXT
);

CREATE TABLE IF NOT EXISTS workflow_versions (
    id                  TEXT PRIMARY KEY,
    workflow_id         TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    version_hash        TEXT NOT NULL,
    file_path           TEXT NOT NULL,
    inputs_schema_json  TEXT,
    outputs_schema_json TEXT,
    tools_used_json     TEXT,
    created_at          TEXT NOT NULL,
    UNIQUE(workflow_id, version_hash)
);

CREATE TABLE IF NOT EXISTS runs (
    id                  TEXT PRIMARY KEY,
    workflow_version_id TEXT NOT NULL REFERENCES workflow_versions(id) ON DELETE CASCADE,
    inputs_json         TEXT,
    confirmed           INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL,       -- completed | failed
    output_json         TEXT,
    started_at          TEXT NOT NULL,
    finished_at         TEXT
);

CREATE TABLE IF NOT EXISTS steps (
    id          TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    connector   TEXT NOT NULL,
    tool        TEXT NOT NULL,
    risk        TEXT NOT NULL,
    payload_json TEXT,
    result_json TEXT,
    status      TEXT NOT NULL,
    duration_ms INTEGER,
    error       TEXT,
    UNIQUE(run_id, seq)
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    """Open the database, creating and migrating the schema if needed."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


def replace_connector(
    conn: sqlite3.Connection,
    *,
    name: str,
    attr: str,
    transport: str,
    config: dict[str, Any],
    status: str,
    detail: str | None,
    tool_count: int,
) -> None:
    """Write a connector row, discarding any tools recorded for it before.

    Discovery is a full refresh: a tool that disappeared from ``tools/list``
    must disappear from the database too, or drift detection would miss it.
    """
    conn.execute("DELETE FROM tools WHERE connector = ?", (name,))
    conn.execute(
        """
        INSERT INTO connectors(name, attr, transport, config_json, status, detail,
                               tool_count, discovered_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            attr = excluded.attr,
            transport = excluded.transport,
            config_json = excluded.config_json,
            status = excluded.status,
            detail = excluded.detail,
            tool_count = excluded.tool_count,
            discovered_at = excluded.discovered_at
        """,
        (name, attr, transport, canonical_json(config), status, detail, tool_count, now_iso()),
    )


def insert_tool(
    conn: sqlite3.Connection,
    *,
    connector: str,
    name: str,
    method: str,
    description: str | None,
    input_schema: dict[str, Any] | None,
    output_schema: dict[str, Any] | None,
    annotations: dict[str, Any] | None,
    risk: str,
    schema_hash: str,
) -> None:
    conn.execute(
        """
        INSERT INTO tools(connector, name, method, description, input_schema_json,
                          output_schema_json, annotations_json, risk, schema_hash)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            connector,
            name,
            method,
            description,
            canonical_json(input_schema or {}),
            canonical_json(output_schema) if output_schema is not None else None,
            canonical_json(annotations) if annotations is not None else None,
            risk,
            schema_hash,
        ),
    )


def prune_connectors(conn: sqlite3.Connection, keep: list[str]) -> None:
    """Drop connectors that are no longer in the config."""
    if keep:
        placeholders = ",".join("?" for _ in keep)
        conn.execute(f"DELETE FROM tools WHERE connector NOT IN ({placeholders})", keep)
        conn.execute(f"DELETE FROM connectors WHERE name NOT IN ({placeholders})", keep)
    else:
        conn.execute("DELETE FROM tools")
        conn.execute("DELETE FROM connectors")
