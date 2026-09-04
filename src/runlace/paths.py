"""Where runlace keeps its state on the user's machine.

Every path derives from the Runlace home directory, which is ``~/.runlace``
unless ``RUNLACE_HOME`` is set. Tests set that variable so they never touch the
real one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RunlacePaths:
    home: Path

    @property
    def db(self) -> Path:
        return self.home / "runlace.db"

    @property
    def config(self) -> Path:
        return self.home / "config.json"

    @property
    def policy(self) -> Path:
        return self.home / "policy.yaml"

    @property
    def model(self) -> Path:
        """The inference backend `ctx.ai` calls. Absent until one is configured.

        Its own file rather than a section of ``config.json``: writing that
        document replaces it whole, so every caller that does not know about a
        model section would erase one.
        """
        return self.home / "model.json"

    @property
    def types(self) -> Path:
        return self.home / "runlace_types"

    @property
    def connectors(self) -> Path:
        return self.types / "connectors"

    @property
    def workflows(self) -> Path:
        return self.home / "workflows"

    def create(self) -> None:
        """Create the directory layout. Safe to call on an existing home."""
        self.home.mkdir(parents=True, exist_ok=True)
        self.connectors.mkdir(parents=True, exist_ok=True)
        self.workflows.mkdir(parents=True, exist_ok=True)


def runlace_home() -> Path:
    override = os.environ.get("RUNLACE_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".runlace"


def paths() -> RunlacePaths:
    return RunlacePaths(runlace_home())
