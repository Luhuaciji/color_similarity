"""Minimal, dependency-free environment configuration.

Secrets are deliberately returned to the caller and are never logged, serialized,
or included in exception messages by this module.
"""

from __future__ import annotations

import os
from pathlib import Path


class MissingEnvironmentVariable(RuntimeError):
    """Raised when a required environment variable is absent."""


def load_env_file(path: Path, *, override: bool = False) -> tuple[str, ...]:
    """Load KEY=VALUE entries from *path* and return the variable names loaded."""

    if not path.exists():
        return ()

    loaded: list[str] = []
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"invalid env syntax at line {line_number}")

        name, raw_value = line.split("=", 1)
        name = name.strip()
        if not name or not name.replace("_", "A").isalnum() or name[0].isdigit():
            raise ValueError(f"invalid env variable name at line {line_number}")

        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if override or name not in os.environ:
            os.environ[name] = value
            loaded.append(name)
    return tuple(loaded)


def require_env(name: str) -> str:
    """Return a non-empty environment variable or fail without exposing values."""

    value = os.environ.get(name, "")
    if not value:
        raise MissingEnvironmentVariable(f"missing required environment variable: {name}")
    return value
