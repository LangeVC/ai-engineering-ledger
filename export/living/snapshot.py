"""Canonical content-addressed snapshot construction for AEL v0.4."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = "0.4"
_ARTIFACT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _artifact_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    return canonical_json(value) + b"\n"


def _content_hash(artifacts: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name in sorted(artifacts):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(artifacts[name])
    return digest.hexdigest()


@dataclass(frozen=True)
class SnapshotDraft:
    manifest: dict[str, Any]
    artifacts: dict[str, bytes]


def build_snapshot(
    artifacts: Mapping[str, Any],
    *,
    filter_spec: Mapping[str, Any],
    transformation_version: str,
    parent_snapshot_id: str | None = None,
    warnings: tuple[str, ...] | list[str] = (),
    created_at: str | None = None,
) -> SnapshotDraft:
    """Build a deterministic draft identity from canonical semantic inputs."""
    if not transformation_version:
        raise ValueError("transformation_version is required")
    if not artifacts:
        raise ValueError("a snapshot requires at least one artifact")
    encoded: dict[str, bytes] = {}
    for original_name, value in artifacts.items():
        name = str(original_name)
        if not _ARTIFACT_NAME.fullmatch(name) or name in {".", ".."}:
            raise ValueError("snapshot artifact name is invalid")
        encoded[name] = _artifact_bytes(value)
    artifact_hash = _content_hash(encoded)
    identity = {
        "schema_version": SCHEMA_VERSION,
        "filter": dict(filter_spec),
        "transformation_version": transformation_version,
        "parent_snapshot_id": parent_snapshot_id,
        "artifact_content_hash": artifact_hash,
    }
    snapshot_id = "snap_" + hashlib.sha256(canonical_json(identity)).hexdigest()
    timestamp = created_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    manifest = {
        "record_type": "study_snapshot",
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "parent_snapshot_id": parent_snapshot_id,
        "status": "draft",
        "created_at": timestamp,
        "finalized_at": None,
        "filter": dict(filter_spec),
        "transformation_version": transformation_version,
        "content_hash": artifact_hash,
        "warnings": sorted(set(str(item) for item in warnings if str(item))),
        "files": [
            {"name": name, "size_bytes": len(encoded[name]), "sha256": hashlib.sha256(encoded[name]).hexdigest()}
            for name in sorted(encoded)
        ],
    }
    return SnapshotDraft(manifest=manifest, artifacts=encoded)
