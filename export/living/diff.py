"""Deterministic semantic diffs for verified living-study snapshots."""
from __future__ import annotations
import hashlib, json
from typing import Any, Mapping
from .snapshot import canonical_json

ALGORITHM_VERSION = "0.4.0"

def _records(value: Any, prefix: str = "") -> dict[str, Any]:
    found: dict[str, Any] = {}
    if isinstance(value, Mapping):
        identity = value.get("id") or value.get("claim_id") or value.get("link_id") or value.get("source_alias")
        if identity is not None:
            found[f"{prefix}:{identity}"] = value
        for key, item in sorted(value.items()):
            found.update(_records(item, f"{prefix}/{key}"))
    elif isinstance(value, list):
        anonymous: dict[str, int] = {}
        for item in value:
            child = _records(item, prefix)
            if child:
                found.update(child)
            else:
                digest = hashlib.sha256(canonical_json(item)).hexdigest()
                ordinal = anonymous.get(digest, 0)
                anonymous[digest] = ordinal + 1
                found[f"{prefix}:content:{digest}:{ordinal}"] = item
    return found

def build_diff(from_manifest: Mapping[str, Any], to_manifest: Mapping[str, Any],
               from_data: Mapping[str, Any], to_data: Mapping[str, Any]) -> dict[str, Any]:
    if from_manifest.get("status") != "finalized" or to_manifest.get("status") != "finalized":
        raise ValueError("diff requires finalized verified snapshots")
    before, after = _records(from_data), _records(to_data)
    changes=[]
    for key in sorted(set(before)|set(after)):
        if key not in before: kind, reason = "added", "record_added"
        elif key not in after: kind, reason = "removed", "record_removed"
        elif canonical_json(before[key]) != canonical_json(after[key]): kind, reason = "changed", "record_content_changed"
        else: kind, reason = "unchanged", "record_unchanged"
        changes.append({"domain": key.split(":",1)[0].lstrip("/") or "root", "record_id": key,
                        "change_type": kind, "reason_code": reason,
                        "before": before.get(key), "after": after.get(key)})
    core={"record_type":"study_diff","schema_version":"0.4","algorithm_version":ALGORITHM_VERSION,
          "from_snapshot_id":from_manifest["snapshot_id"],"to_snapshot_id":to_manifest["snapshot_id"],
          "changes":changes}
    return {**core,"content_hash":hashlib.sha256(canonical_json(core)).hexdigest()}

