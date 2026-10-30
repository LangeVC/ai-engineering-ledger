"""Credential-free adapter manifest and conformance helpers."""
from __future__ import annotations
import hashlib, json
from typing import Any, Iterable

STATES={"value","partial","unknown","unsupported","unauthorized","unverified","evidenced_zero"}
def validate_manifest(manifest: dict[str,Any]) -> dict[str,Any]:
    required={"schema_version","adapter_id","source","surface","capabilities","checkpoint_semantics","evidence_states"}
    if not required <= set(manifest): raise ValueError("adapter manifest omits required fields")
    if manifest["schema_version"]!="0.4": raise ValueError("unsupported adapter manifest version")
    if not set(manifest["evidence_states"]) <= STATES: raise ValueError("unknown evidence state")
    return manifest
def fixture_observations(adapter_id: str, rows: Iterable[dict[str,Any]]) -> list[dict[str,Any]]:
    result=[]
    for row in rows:
        if any(key.lower() in {"secret","token","password","api_key"} for key in row): raise ValueError("fixture contains secret-like field")
        core={**row,"adapter_id":adapter_id}
        core["idempotency_key"]=hashlib.sha256(json.dumps(core,sort_keys=True,separators=(",",":")).encode()).hexdigest()
        result.append(core)
    return sorted(result,key=lambda item:item["idempotency_key"])

