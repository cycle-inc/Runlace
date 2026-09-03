"""Tool risk classification (D5).

MCP's ``readOnlyHint`` annotation decides when it is present. Anything
unannotated is treated as a side effect. Per-tool overrides in ``policy.yaml``
are a later milestone; this module is the classifier they will wrap.
"""

from __future__ import annotations

from typing import Any, Literal

Risk = Literal["read_only", "side_effect"]

READ_ONLY: Risk = "read_only"
SIDE_EFFECT: Risk = "side_effect"


def classify(annotations: dict[str, Any] | None) -> Risk:
    if annotations is not None and "readOnlyHint" in annotations:
        return READ_ONLY if annotations["readOnlyHint"] else SIDE_EFFECT
    return SIDE_EFFECT
