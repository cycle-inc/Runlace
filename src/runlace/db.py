"""SQLite storage.

The four workflow tables come straight from the spec. ``connectors`` and
``tools`` are Runlace's own: discovery has to persist somewhere, and D10 puts
everything but workflow code in the database.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .hashing import canonical_json

Connection = sqlite3.Connection

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
    status              TEXT NOT NULL,       -- running | completed | failed
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


# SCHEMA above is version 1. Every change since is a statement here, applied in
# order to whatever version a database is already at. Only additive changes
# belong in this list: a Runlace home is the user's data.
MIGRATIONS = [
    # v2 (M3): why a run failed or was refused. `status` says that it did; this
    # says what to tell the human.
    "ALTER TABLE runs ADD COLUMN error TEXT",
]

SCHEMA_VERSION = 1 + len(MIGRATIONS)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    """Open the database, creating and migrating the schema if needed."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing database up to :data:`SCHEMA_VERSION`.

    A database that ``executescript(SCHEMA)`` just created is at version 1, the
    same as one written by an older Runlace, so both take the same path.
    """
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    current = int(row["value"]) if row is not None else 1
    for statement in MIGRATIONS[current - 1 :]:
        conn.execute(statement)


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


def find_workflow(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    """Look a workflow up by id or by name -- agents reliably have one or the other."""
    return conn.execute(
        "SELECT * FROM workflows WHERE id = ? OR name = ?", (key, key)
    ).fetchone()


def insert_workflow(
    conn: sqlite3.Connection, *, workflow_id: str, name: str, description: str | None
) -> None:
    conn.execute(
        "INSERT INTO workflows(id, name, description) VALUES(?, ?, ?)",
        (workflow_id, name, description),
    )


def update_workflow_description(
    conn: sqlite3.Connection, workflow_id: str, description: str | None
) -> None:
    conn.execute(
        "UPDATE workflows SET description = ? WHERE id = ?", (description, workflow_id)
    )


def find_version_by_hash(
    conn: sqlite3.Connection, workflow_id: str, version_hash: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM workflow_versions WHERE workflow_id = ? AND version_hash = ?",
        (workflow_id, version_hash),
    ).fetchone()


def find_version(
    conn: sqlite3.Connection, workflow_id: str, key: str
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM workflow_versions
        WHERE workflow_id = ? AND (id = ? OR version_hash = ?)
        """,
        (workflow_id, key, key),
    ).fetchone()


def list_versions(conn: sqlite3.Connection, workflow_id: str) -> list[sqlite3.Row]:
    """Versions oldest first.

    Ordered by rowid rather than ``created_at``: timestamps have second
    resolution, and two versions of the same workflow easily land in the same
    second.
    """
    return list(
        conn.execute(
            "SELECT * FROM workflow_versions WHERE workflow_id = ? ORDER BY rowid",
            (workflow_id,),
        )
    )


def insert_workflow_version(
    conn: sqlite3.Connection,
    *,
    version_id: str,
    workflow_id: str,
    version_hash: str,
    file_path: str,
    inputs_schema: dict[str, Any] | None,
    outputs_schema: dict[str, Any] | None,
    tools_used: list[dict[str, Any]],
) -> None:
    """Add a version. Versions are immutable: nothing ever updates this row."""
    conn.execute(
        """
        INSERT INTO workflow_versions(id, workflow_id, version_hash, file_path,
                                      inputs_schema_json, outputs_schema_json,
                                      tools_used_json, created_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            version_id,
            workflow_id,
            version_hash,
            file_path,
            canonical_json(inputs_schema) if inputs_schema is not None else None,
            canonical_json(outputs_schema) if outputs_schema is not None else None,
            canonical_json(tools_used),
            now_iso(),
        ),
    )
    conn.execute(
        "UPDATE workflows SET latest_version = ? WHERE id = ?",
        (version_id, workflow_id),
    )


def list_workflow_summaries(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """One row per workflow, with what `list_workflows` reports."""
    return list(
        conn.execute(
            """
            SELECT w.id            AS id,
                   w.name          AS name,
                   w.description   AS description,
                   v.version_hash  AS latest_version,
                   v.created_at    AS latest_created_at,
                   (SELECT MIN(created_at) FROM workflow_versions
                     WHERE workflow_id = w.id)          AS created_at,
                   (SELECT COUNT(*) FROM workflow_versions
                     WHERE workflow_id = w.id)          AS version_count,
                   (SELECT r.status FROM runs r
                      JOIN workflow_versions rv ON rv.id = r.workflow_version_id
                     WHERE rv.workflow_id = w.id
                     ORDER BY r.started_at DESC, r.rowid DESC LIMIT 1) AS last_run_status,
                   (SELECT r.started_at FROM runs r
                      JOIN workflow_versions rv ON rv.id = r.workflow_version_id
                     WHERE rv.workflow_id = w.id
                     ORDER BY r.started_at DESC, r.rowid DESC LIMIT 1) AS last_run_at
            FROM workflows w
            LEFT JOIN workflow_versions v ON v.id = w.latest_version
            ORDER BY w.name
            """
        )
    )


def find_tool_by_method(
    conn: sqlite3.Connection, attr: str, method: str
) -> sqlite3.Row | None:
    """Resolve ``ctx.<attr>.<method>`` to the connector and tool it stands for."""
    return conn.execute(
        """
        SELECT c.name AS connector, t.name AS tool, t.risk AS risk,
               t.input_schema_json AS input_schema_json
        FROM tools t
        JOIN connectors c ON c.name = t.connector
        WHERE c.attr = ? AND t.method = ?
        """,
        (attr, method),
    ).fetchone()


# -- runs and steps --------------------------------------------------------


def insert_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    workflow_version_id: str,
    inputs: Any,
    confirmed: bool,
) -> None:
    """Open a run. Written before anything is attempted, so a crash leaves a trace."""
    conn.execute(
        """
        INSERT INTO runs(id, workflow_version_id, inputs_json, confirmed, status,
                         started_at)
        VALUES(?, ?, ?, ?, 'running', ?)
        """,
        (run_id, workflow_version_id, canonical_json(inputs), int(confirmed), now_iso()),
    )


def finish_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    status: str,
    output: Any = None,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE runs SET status = ?, output_json = ?, error = ?, finished_at = ?
        WHERE id = ?
        """,
        (
            status,
            canonical_json(output) if output is not None else None,
            error,
            now_iso(),
            run_id,
        ),
    )


def insert_step(
    conn: sqlite3.Connection,
    *,
    step_id: str,
    run_id: str,
    seq: int,
    connector: str,
    tool: str,
    risk: str,
    payload: Any,
    result: Any,
    status: str,
    duration_ms: int,
    error: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO steps(id, run_id, seq, connector, tool, risk, payload_json,
                          result_json, status, duration_ms, error)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            step_id,
            run_id,
            seq,
            connector,
            tool,
            risk,
            canonical_json(payload),
            canonical_json(result) if result is not None else None,
            status,
            duration_ms,
            error,
        ),
    )


def find_run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def list_steps(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute("SELECT * FROM steps WHERE run_id = ? ORDER BY seq", (run_id,))
    )


def tool_schema_hashes(conn: sqlite3.Connection) -> dict[tuple[str, str], str]:
    """Current ``(connector, tool) -> schema_hash``, for drift comparison."""
    return {
        (str(row["connector"]), str(row["name"])): str(row["schema_hash"])
        for row in conn.execute("SELECT connector, name, schema_hash FROM tools")
    }


def prune_connectors(conn: sqlite3.Connection, keep: list[str]) -> None:
    """Drop connectors that are no longer in the config."""
    if keep:
        placeholders = ",".join("?" for _ in keep)
        conn.execute(f"DELETE FROM tools WHERE connector NOT IN ({placeholders})", keep)
        conn.execute(f"DELETE FROM connectors WHERE name NOT IN ({placeholders})", keep)
    else:
        conn.execute("DELETE FROM tools")
        conn.execute("DELETE FROM connectors")
