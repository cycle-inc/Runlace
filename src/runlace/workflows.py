"""Storing workflows: immutable versions, on-disk code, drift status.

D1 makes versions immutable and content-addressed. Nothing in this module ever
updates a ``workflow_versions`` row or overwrites a workflow file; creating
under an existing name adds a version and moves the ``latest_version`` pointer.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .compiler import CompileError, CompileResult, compile_workflow
from .db import (
    Connection,
    find_version,
    find_version_by_hash,
    find_workflow,
    insert_workflow,
    insert_workflow_version,
    list_versions,
    list_workflow_summaries,
    tool_schema_hashes,
    update_workflow_description,
)
from .hashing import version_hash as compute_version_hash
from .paths import RunlacePaths

# Workflow names become directory names, so they stay boring on purpose.
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")

# Enough of the content hash to be unique per workflow, short enough to paste.
VERSION_LENGTH = 16

STAGE_VALIDATE = "validate"


@dataclass
class CreateResult:
    ok: bool
    stage: str | None = None
    errors: list[CompileError] = field(default_factory=list[CompileError])
    warnings: list[str] = field(default_factory=list[str])
    workflow_id: str | None = None
    version: str | None = None
    version_id: str | None = None
    tools_used: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    created: bool = False

    def to_json(self) -> dict[str, Any]:
        if not self.ok:
            return {
                "ok": False,
                "stage": self.stage,
                "errors": [e.to_json() for e in self.errors],
            }
        return {
            "ok": True,
            "workflow_id": self.workflow_id,
            "version": self.version,
            "tools_used": self.tools_used,
            "warnings": self.warnings,
        }


def create_workflow(
    conn: Connection,
    paths: RunlacePaths,
    *,
    name: str,
    description: str | None,
    code: str,
    inputs_schema: dict[str, Any] | None,
    outputs_schema: dict[str, Any] | None = None,
) -> CreateResult:
    """Compile a workflow and, if it passes, store it as a new immutable version."""
    validation = _validate(name, inputs_schema, outputs_schema)
    if validation:
        return CreateResult(ok=False, stage=STAGE_VALIDATE, errors=validation)

    compiled = compile_workflow(
        conn,
        paths.types,
        name=name,
        code=code,
        inputs_schema=inputs_schema,
        outputs_schema=outputs_schema,
    )
    if not compiled.ok:
        return CreateResult(ok=False, stage=compiled.stage, errors=compiled.errors)

    return _store(
        conn,
        paths,
        name=name,
        description=description,
        code=code,
        inputs_schema=inputs_schema,
        outputs_schema=outputs_schema,
        compiled=compiled,
    )


def _validate(
    name: str,
    inputs_schema: dict[str, Any] | None,
    outputs_schema: dict[str, Any] | None,
) -> list[CompileError]:
    errors: list[CompileError] = []
    if not NAME_PATTERN.match(name):
        errors.append(
            CompileError(
                STAGE_VALIDATE,
                None,
                f"`{name}` is not a usable workflow name",
                "Use lowercase letters, digits, hyphens and underscores, starting "
                "with a letter or digit -- the name becomes a directory. For "
                "example: monthly-expense-report.",
                "bad-name",
            )
        )
    for label, schema in (("inputs_schema", inputs_schema), ("outputs_schema", outputs_schema)):
        if schema is not None and not isinstance(schema, dict):
            errors.append(
                CompileError(
                    STAGE_VALIDATE,
                    None,
                    f"{label} must be a JSON Schema object",
                    'Pass something like {"type": "object", "properties": {...}, '
                    '"required": [...]}.',
                    "bad-schema",
                )
            )
    return errors


def _store(
    conn: Connection,
    paths: RunlacePaths,
    *,
    name: str,
    description: str | None,
    code: str,
    inputs_schema: dict[str, Any] | None,
    outputs_schema: dict[str, Any] | None,
    compiled: CompileResult,
) -> CreateResult:
    version = compute_version_hash(code, inputs_schema, outputs_schema)[:VERSION_LENGTH]
    tools_used = [t.to_json() for t in compiled.tools_used]

    row = find_workflow(conn, name)
    if row is None:
        workflow_id = f"wf_{uuid.uuid4().hex[:12]}"
        insert_workflow(
            conn, workflow_id=workflow_id, name=name, description=description
        )
    else:
        workflow_id = str(row["id"])
        if description is not None and description != row["description"]:
            update_workflow_description(conn, workflow_id, description)

    existing = find_version_by_hash(conn, workflow_id, version)
    if existing is not None:
        # Same code and same schemas: the version already exists, and D1 forbids
        # overwriting it. Report it rather than inventing a duplicate.
        conn.commit()
        return CreateResult(
            ok=True,
            workflow_id=workflow_id,
            version=version,
            version_id=str(existing["id"]),
            tools_used=tools_used,
            warnings=[
                *compiled.warnings,
                f"Identical to existing version {version}; no new version was "
                f"created.",
            ],
            created=False,
        )

    directory = paths.workflows / name
    directory.mkdir(parents=True, exist_ok=True)
    file_path = directory / f"{version}.py"
    file_path.write_text(code, encoding="utf-8")

    version_id = f"wv_{uuid.uuid4().hex[:12]}"
    insert_workflow_version(
        conn,
        version_id=version_id,
        workflow_id=workflow_id,
        version_hash=version,
        file_path=str(file_path),
        inputs_schema=inputs_schema,
        outputs_schema=outputs_schema,
        tools_used=tools_used,
    )
    conn.commit()

    return CreateResult(
        ok=True,
        workflow_id=workflow_id,
        version=version,
        version_id=version_id,
        tools_used=tools_used,
        warnings=list(compiled.warnings),
        created=True,
    )


# -- reading ---------------------------------------------------------------


def list_workflows(conn: Connection) -> list[dict[str, Any]]:
    return [
        {
            "workflow_id": str(row["id"]),
            "name": str(row["name"]),
            "description": row["description"],
            "latest_version": row["latest_version"],
            "versions": int(row["version_count"]),
            "created_at": row["created_at"],
            "updated_at": row["latest_created_at"],
            "last_run": (
                None
                if row["last_run_status"] is None
                else {"status": row["last_run_status"], "at": row["last_run_at"]}
            ),
        }
        for row in list_workflow_summaries(conn)
    ]


def get_workflow(
    conn: Connection, key: str, *, version: str | None = None
) -> dict[str, Any] | None:
    """The full record for one workflow, including its code and drift status."""
    row = find_workflow(conn, key)
    if row is None:
        return None
    workflow_id = str(row["id"])

    wanted = version or row["latest_version"]
    version_row = (
        find_version(conn, workflow_id, str(wanted)) if wanted is not None else None
    )
    if version_row is None:
        return {
            "workflow_id": workflow_id,
            "name": str(row["name"]),
            "description": row["description"],
            "version": None,
            "versions": [],
            "error": f"workflow `{row['name']}` has no version {version}"
            if version
            else f"workflow `{row['name']}` has no versions",
        }

    tools_used = _json_list(version_row["tools_used_json"])
    return {
        "workflow_id": workflow_id,
        "name": str(row["name"]),
        "description": row["description"],
        "version": str(version_row["version_hash"]),
        "version_id": str(version_row["id"]),
        "is_latest": str(version_row["id"]) == str(row["latest_version"]),
        "created_at": version_row["created_at"],
        "file_path": str(version_row["file_path"]),
        "code": _read_code(Path(str(version_row["file_path"]))),
        "inputs_schema": _json_object(version_row["inputs_schema_json"]),
        "outputs_schema": _json_object(version_row["outputs_schema_json"]),
        "tools_used": tools_used,
        "drift": schema_drift(conn, tools_used),
        "versions": [
            {
                "version": str(v["version_hash"]),
                "created_at": v["created_at"],
                "is_latest": str(v["id"]) == str(row["latest_version"]),
            }
            for v in list_versions(conn, workflow_id)
        ],
    }


def schema_drift(conn: Connection, tools_used: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare each pinned schema hash against what discovery currently sees.

    A workflow whose tools have drifted is still stored and still readable; the
    refusal to run it is D6's job in M3. Reporting it here is what lets an agent
    decide to re-create the workflow before trying.
    """
    current = tool_schema_hashes(conn)
    changed: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for tool in tools_used:
        key = (str(tool.get("connector")), str(tool.get("tool")))
        now = current.get(key)
        if now is None:
            missing.append({"connector": key[0], "tool": key[1]})
        elif now != tool.get("schema_hash"):
            changed.append(
                {
                    "connector": key[0],
                    "tool": key[1],
                    "pinned": tool.get("schema_hash"),
                    "current": now,
                }
            )
    return {
        "ok": not changed and not missing,
        "changed": changed,
        "missing": missing,
    }


def _read_code(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _json_object(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, str):
        return None
    value = json.loads(raw)
    return value if isinstance(value, dict) else None


def _json_list(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, str):
        return []
    value = json.loads(raw)
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []
