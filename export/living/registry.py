"""Content-addressed, immutable local snapshot registry."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .snapshot import SnapshotDraft, canonical_json


@dataclass(frozen=True)
class SnapshotVerification:
    schema: bool
    privacy: bool
    hashes: bool
    replay: bool

    @classmethod
    def valid(cls) -> "SnapshotVerification":
        return cls(schema=True, privacy=True, hashes=True, replay=True)

    @property
    def passed(self) -> bool:
        return self.schema and self.privacy and self.hashes and self.replay

    def as_dict(self) -> dict[str, bool]:
        return {
            "schema": self.schema,
            "privacy": self.privacy,
            "hashes": self.hashes,
            "replay": self.replay,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class SnapshotRegistry:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        if self.root.is_symlink():
            raise ValueError("refusing symlinked snapshot registry")
        self.snapshots = self.root / "snapshots"
        self.pointers = self.root / "pointers"
        self.snapshots.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.pointers.mkdir(parents=True, exist_ok=True, mode=0o700)

    @staticmethod
    def _write_new(path: Path, raw: bytes) -> None:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, raw)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _replace(path: Path, raw: bytes) -> None:
        temp = path.with_name("." + path.name + "." + secrets.token_hex(8))
        SnapshotRegistry._write_new(temp, raw)
        os.replace(temp, path)

    def _directory(self, snapshot_id: str) -> Path:
        if not snapshot_id.startswith("snap_") or any(char not in "0123456789abcdef" for char in snapshot_id[5:]):
            raise ValueError("invalid snapshot ID")
        return self.snapshots / snapshot_id

    def finalize(
        self,
        draft: SnapshotDraft,
        verification: SnapshotVerification,
        *,
        update_latest: bool = True,
    ) -> dict[str, Any]:
        if not verification.passed:
            raise ValueError("snapshot verification did not pass")
        snapshot_id = str(draft.manifest["snapshot_id"])
        target = self._directory(snapshot_id)
        manifest_path = target / "manifest.json"
        if target.exists():
            existing = self.verify(snapshot_id)
            if existing["content_hash"] != draft.manifest["content_hash"]:
                raise ValueError("finalized snapshot ID cannot be reused with different content")
            if update_latest:
                self._update_latest(existing)
            return existing

        staging = self.snapshots / ("." + snapshot_id + "." + secrets.token_hex(8))
        staging.mkdir(mode=0o700)
        try:
            for name, raw in sorted(draft.artifacts.items()):
                self._write_new(staging / name, raw)
            manifest = {
                **draft.manifest,
                "status": "finalized",
                "finalized_at": _now(),
                "verification": verification.as_dict(),
            }
            manifest_raw = canonical_json(manifest) + b"\n"
            self._write_new(staging / "manifest.json", manifest_raw)
            os.replace(staging, target)
        except Exception:
            if staging.exists():
                for child in staging.iterdir():
                    if child.is_file():
                        child.unlink()
                staging.rmdir()
            raise
        verified = self.verify(snapshot_id)
        if update_latest:
            self._update_latest(verified)
        return verified

    def _update_latest(self, manifest: dict[str, Any]) -> None:
        if manifest.get("status") != "finalized":
            raise ValueError("latest requires a finalized snapshot")
        raw = canonical_json(
            {
                "schema_version": "0.4",
                "pointer": "latest",
                "snapshot_id": manifest["snapshot_id"],
                "content_hash": manifest["content_hash"],
                "updated_at": _now(),
            }
        ) + b"\n"
        self._replace(self.pointers / "latest.json", raw)

    def latest(self) -> dict[str, Any]:
        path = self.pointers / "latest.json"
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError("latest snapshot pointer does not exist")
        pointer = json.loads(path.read_text())
        manifest = self.verify(pointer["snapshot_id"])
        if manifest["content_hash"] != pointer["content_hash"]:
            raise ValueError("latest pointer content hash does not match snapshot")
        return manifest

    def list(self) -> list[dict[str, Any]]:
        values = []
        for path in sorted(self.snapshots.glob("snap_*/manifest.json")):
            values.append(self.verify(path.parent.name))
        return values

    def verify(self, snapshot_id: str) -> dict[str, Any]:
        target = self._directory(snapshot_id)
        manifest_path = target / "manifest.json"
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise ValueError("snapshot manifest is missing or unsafe")
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("snapshot_id") != snapshot_id or manifest.get("status") != "finalized":
            raise ValueError("snapshot manifest identity or status is invalid")
        artifacts: dict[str, bytes] = {}
        for item in manifest.get("files", []):
            name = str(item["name"])
            path = target / name
            if path.parent != target or not path.is_file() or path.is_symlink():
                raise ValueError("snapshot artifact is missing or unsafe")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != item["sha256"]:
                raise ValueError("snapshot artifact hash verification failed")
            artifacts[name] = raw
        digest = hashlib.sha256()
        for name in sorted(artifacts):
            digest.update(name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(artifacts[name])
        if digest.hexdigest() != manifest.get("content_hash"):
            raise ValueError("snapshot content hash verification failed")
        return manifest

