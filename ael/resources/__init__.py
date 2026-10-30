"""Installed, versioned study-contract resources."""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any


RESOURCE_NAMES = frozenset(
    {
        "study-v0.3.schema.json",
        "study_scope.json",
        "study_repository_map.json",
    }
)


def read_text(name: str) -> str:
    """Read one packaged resource without relying on checkout-relative paths."""
    if name not in RESOURCE_NAMES:
        raise ValueError(f"unknown AEL resource: {name}")
    return files(__package__).joinpath(name).read_text(encoding="utf-8")


def load_json(name: str) -> Any:
    """Decode one packaged JSON resource."""
    return json.loads(read_text(name))


__all__ = ["RESOURCE_NAMES", "load_json", "read_text"]
