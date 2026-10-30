"""Shared provenance-complete evidence-domain contract."""
from __future__ import annotations
import hashlib, json
from datetime import datetime
from typing import Any

def normalize_domain_record(domain: str, record: dict[str, Any]) -> dict[str, Any]:
    if domain not in {"process","infrastructure","capability"}: raise ValueError("unsupported evidence domain")
    interval=record.get("interval",{}); start=interval.get("start"); end=interval.get("end_exclusive")
    if not start or not end or datetime.fromisoformat(start.replace("Z","+00:00")) >= datetime.fromisoformat(end.replace("Z","+00:00")):
        raise ValueError("evidence domain requires a non-empty interval")
    confidence=record.get("confidence")
    if isinstance(confidence,bool) or not isinstance(confidence,(int,float)) or not 0 <= confidence <= 1:
        raise ValueError("confidence must be between zero and one")
    coverage=record.get("coverage")
    if coverage not in {"complete","partial","unknown","unsupported","unverified","unavailable"}:
        raise ValueError("unsupported coverage state")
    provenance=record.get("provenance")
    if not isinstance(provenance,dict) or not provenance.get("method") or not isinstance(provenance.get("evidence_ids"),list):
        raise ValueError("provenance method and evidence IDs are required")
    core={"schema_version":"0.4","domain":domain,"interval":{"start":start,"end_exclusive":end},
          "confidence":confidence,"coverage":coverage,"provenance":provenance,"payload":record.get("payload",{})}
    core["record_id"]=record.get("record_id") or domain+"_"+hashlib.sha256(json.dumps(core,sort_keys=True,separators=(",",":")).encode()).hexdigest()[:24]
    return core

