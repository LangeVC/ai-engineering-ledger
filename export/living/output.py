"""Additive v0.4 data-only output composed from canonical v0.3 study data."""
from __future__ import annotations
import hashlib
from typing import Any, Mapping
from .snapshot import canonical_json

def build_living_export(*, study_v03: Mapping[str, Any], snapshot: Mapping[str, Any],
                        source_health: list[Mapping[str, Any]], diff: Mapping[str, Any] | None = None,
                        changelog: list[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    if study_v03.get("schema_version") != "0.3":
        raise ValueError("living export requires canonical v0.3 study data")
    if snapshot.get("status") != "finalized":
        raise ValueError("living export requires finalized snapshot")
    core={"record_type":"living_study_data_export","schema_version":"0.4",
          "contract":"ael.study.data-export@0.4",
          "compatibility":{"source_contract":"ael.study.data-export@0.3","source_content_hash":study_v03.get("content_hash")},
          "snapshot":{"snapshot_id":snapshot["snapshot_id"],"content_hash":snapshot["content_hash"],
                      "transformation_version":snapshot["transformation_version"]},
          "active_filter":study_v03["active_filter"],"scope":study_v03["scope"],
          "study":dict(study_v03),"source_health":sorted((dict(v) for v in source_health),key=canonical_json),
          "diff":dict(diff) if diff else None,"changelog":list(changelog or [])}
    return {**core,"content_hash":hashlib.sha256(canonical_json(core)).hexdigest()}

def build_changelog(diff: Mapping[str, Any]) -> list[dict[str, Any]]:
    counts={}
    for change in diff.get("changes",[]):
        key=(change["domain"],change["change_type"],change["reason_code"])
        counts[key]=counts.get(key,0)+1
    return [{"domain":k[0],"change_type":k[1],"reason_code":k[2],"count":v}
            for k,v in sorted(counts.items())]

