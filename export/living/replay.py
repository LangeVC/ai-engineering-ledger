"""Replay and privacy verification for finalized living-study artifacts."""
from __future__ import annotations
from pathlib import Path
from typing import Iterable
from export.privacy import PrivacyProfile, scan_artifacts
from .registry import SnapshotRegistry

def verify_snapshot(root: str | Path, snapshot_id: str, *, approved_aliases: Iterable[str] = ()) -> dict:
    registry=SnapshotRegistry(root)
    manifest=registry.verify(snapshot_id)
    directory=Path(root)/"snapshots"/snapshot_id
    artifacts={item["name"]:(directory/item["name"]).read_bytes() for item in manifest["files"]}
    report=scan_artifacts(artifacts,profile=PrivacyProfile.create("living-study",list(approved_aliases)))
    if not report["passed"]:
        raise ValueError("snapshot privacy verification failed: "+",".join(report["blocked_artifacts"]))
    return {"snapshot_id":snapshot_id,"content_hash":manifest["content_hash"],"privacy":report,"replay":"passed"}

