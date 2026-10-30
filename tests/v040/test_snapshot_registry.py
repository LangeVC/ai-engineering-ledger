from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from export.living import SnapshotRegistry, SnapshotVerification, build_snapshot


def draft(value: int = 1, warnings: tuple[str, ...] = ()):
    return build_snapshot(
        {"study.json": {"value": value}},
        filter_spec={"start": "2026-04-26", "end_exclusive": "2026-11-01"},
        transformation_version="0.4.0",
        warnings=warnings,
        created_at="2026-10-30T12:00:00Z",
    )


def test_finalized_snapshot_is_write_once_and_latest_is_atomic(tmp_path: Path) -> None:
    registry = SnapshotRegistry(tmp_path)
    manifest = registry.finalize(draft(), SnapshotVerification.valid())
    assert manifest["status"] == "finalized"
    assert registry.latest()["snapshot_id"] == manifest["snapshot_id"]
    assert registry.finalize(draft(), SnapshotVerification.valid()) == manifest
    schema = json.loads((Path(__file__).parents[2] / "ael" / "resources" / "study-snapshot-v0.4.schema.json").read_text())
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(manifest)


def test_invalid_snapshot_cannot_move_latest(tmp_path: Path) -> None:
    registry = SnapshotRegistry(tmp_path)
    first = registry.finalize(draft(), SnapshotVerification.valid())
    with pytest.raises(ValueError, match="verification"):
        registry.finalize(draft(2), SnapshotVerification(schema=False, privacy=True, hashes=True, replay=True))
    assert registry.latest()["snapshot_id"] == first["snapshot_id"]


def test_warning_only_valid_snapshot_can_be_latest_and_tampering_is_detected(tmp_path: Path) -> None:
    registry = SnapshotRegistry(tmp_path)
    manifest = registry.finalize(draft(warnings=("partial coverage",)), SnapshotVerification.valid())
    assert manifest["warnings"] == ["partial coverage"]
    artifact = tmp_path / "snapshots" / manifest["snapshot_id"] / "study.json"
    artifact.write_text('{"tampered":true}')
    with pytest.raises(ValueError, match="hash"):
        registry.verify(manifest["snapshot_id"])

