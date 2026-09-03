"""Per-tool schema hashing.

The hash covers only the typed contract a workflow is compiled against: the
tool's input schema and output schema. Descriptions and annotations are
deliberately excluded, so a server rewording a description does not invalidate
stored workflows, while a changed parameter does.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    """Stable JSON text: sorted keys, no incidental whitespace."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def schema_hash(
    input_schema: dict[str, Any] | None,
    output_schema: dict[str, Any] | None,
) -> str:
    payload = {"inputSchema": input_schema, "outputSchema": output_schema}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
