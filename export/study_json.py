"""Canonical filtered study-data export for AEL v0.3."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

from export.study.writer import _safe_nested


SCHEMA_VERSION = "0.3"
NON_VALUE_STATES = {"unknown", "unsupported", "unverified", "unavailable"}
SEMANTIC_SET_FIELDS = {"evidence_ids", "warning_codes", "limitations", "warnings", "operand_units"}


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _normalize_semantic_sets(value: Any, *, field: str | None = None) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _normalize_semantic_sets(item, field=str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        normalized = [_normalize_semantic_sets(item) for item in value]
        if field in SEMANTIC_SET_FIELDS:
            unique = {_canonical(item): item for item in normalized}
            return [unique[key] for key in sorted(unique)]
        return normalized
    return value


def _normalize_dimension(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _normalize_dimension(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        normalized = [_normalize_dimension(item) for item in value]
        unique = {_canonical(item): item for item in normalized}
        return [unique[key] for key in sorted(unique)]
    return value


def _instant(value: Any, timezone_name: str) -> str:
    text = str(value)
    if "T" not in text:
        parsed = datetime.combine(datetime.fromisoformat(text).date(), time.min, ZoneInfo(timezone_name))
    else:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("study filter timestamps must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_filter(filter_spec: Mapping[str, Any], timezone_name: str) -> dict[str, Any]:
    start = filter_spec.get("start", filter_spec.get("from"))
    end = filter_spec.get("end_exclusive", filter_spec.get("to"))
    if start is None or end is None:
        raise ValueError("active filter requires start and end_exclusive")
    normalized_start = _instant(start, timezone_name)
    normalized_end = _instant(end, timezone_name)
    if normalized_start >= normalized_end:
        raise ValueError("active filter must be a non-empty half-open interval")
    dimensions: dict[str, Any] = {}
    for key, value in filter_spec.items():
        if key in {"start", "from", "end_exclusive", "to", "semantics", "interval", "timezone"}:
            continue
        dimensions[key] = _normalize_dimension(value)
    return {
        "start": normalized_start,
        "end_exclusive": normalized_end,
        "semantics": "[start,end_exclusive)",
        "dimensions": dimensions,
    }


def normalize_study_filter(filter_spec: Mapping[str, Any], timezone_name: str) -> dict[str, Any]:
    """Return the shared canonical filter form used by dashboard and data export."""
    return _normalize_filter(filter_spec, timezone_name)


def _verify_claim_bundle(bundle: Mapping[str, Any]) -> dict[str, Any]:
    content_hash = bundle.get("content_hash")
    if not isinstance(content_hash, str) or len(content_hash) != 64:
        raise ValueError("claims bundle has no valid content hash")
    core = {key: value for key, value in bundle.items() if key != "content_hash"}
    if hashlib.sha256(_canonical(core)).hexdigest() != content_hash:
        raise ValueError("claims bundle content hash verification failed")
    return core


def _scope_filter(scope: Mapping[str, Any]) -> dict[str, Any]:
    interval = scope.get("interval")
    if not isinstance(interval, Mapping):
        raise ValueError("claims bundle has no scoped interval")
    normalized = _normalize_filter(interval, str(interval.get("timezone", "UTC")))
    dimensions = scope.get("dimensions")
    if not isinstance(dimensions, Mapping) or not isinstance(dimensions.get("filter"), Mapping):
        raise ValueError("analytic scope has no bound filter dimensions")
    normalized["dimensions"] = _normalize_dimension(dict(dimensions["filter"]))
    return normalized


def _normalize_domain(name: str, domain: Any) -> dict[str, Any]:
    if not isinstance(domain, Mapping) or not isinstance(domain.get("records"), list):
        raise ValueError(f"analysis domain {name} is malformed")
    status = domain.get("status")
    if status not in {"complete", "partial", *NON_VALUE_STATES}:
        raise ValueError(f"analysis domain {name} has invalid coverage status")
    records: list[dict[str, Any]] = []
    for original in domain["records"]:
        if not isinstance(original, Mapping):
            raise ValueError(f"analysis domain {name} contains a non-object record")
        record = _normalize_semantic_sets(dict(original))
        state = record.get("state")
        if "value" in record and state is None:
            raise ValueError(f"analysis domain {name} has a value without explicit state")
        if state in NON_VALUE_STATES and record.get("value") is not None:
            raise ValueError(f"analysis domain {name} encodes {state} as a value")
        if state == "partial" and "value" not in record:
            raise ValueError(f"analysis domain {name} has a partial record without value")
        records.append(record)
    records.sort(key=_canonical)
    return {"status": status, "records": records}


def _normalize_analytic(record: Any) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise ValueError("claim and correlation records must be objects")
    normalized = _normalize_semantic_sets(dict(record))
    required = {"id", "formula", "scope", "evidence_ids", "coverage", "confidence", "unit", "value", "value_state"}
    missing = required - set(normalized)
    if missing:
        raise ValueError(f"analytic record omits required fields: {', '.join(sorted(missing))}")
    state = normalized["value_state"]
    if state in NON_VALUE_STATES and normalized["value"] is not None:
        raise ValueError(f"analytic state {state} must remain explicit and null")
    if normalized["coverage"].get("status") == "partial":
        warning_codes = normalized["coverage"].get("warning_codes", [])
        if not warning_codes:
            raise ValueError("partial analytic coverage requires machine-readable warnings")
    return normalized


def build_study_json(
    *,
    analysis_bundle: Mapping[str, Any],
    claims_bundle: Mapping[str, Any],
    active_filter: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a deterministic data-only export bound to one active dashboard filter."""
    if analysis_bundle.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("study JSON requires analysis schema version 0.3")
    timezone_name = str(analysis_bundle.get("timezone", "UTC"))
    normalized_filter = _normalize_filter(active_filter, timezone_name)
    analysis_filter = _normalize_filter(analysis_bundle.get("filters", {}), timezone_name)
    if normalized_filter != analysis_filter:
        raise ValueError("active filter does not match the filtered analysis dataset")

    claims_core = _verify_claim_bundle(claims_bundle)
    if _scope_filter(claims_core.get("scope", {})) != normalized_filter:
        raise ValueError("claims bundle scope does not match the active filter")

    domains = {
        str(name): _normalize_domain(str(name), domain)
        for name, domain in sorted(analysis_bundle.get("domains", {}).items())
    }
    claims = sorted(
        (_normalize_analytic(record) for record in claims_core.get("claims", [])),
        key=lambda record: str(record["id"]),
    )
    correlations = sorted(
        (_normalize_analytic(record) for record in claims_core.get("correlations", [])),
        key=lambda record: str(record["id"]),
    )
    for record in claims + correlations:
        if _scope_filter(record["scope"]) != normalized_filter:
            raise ValueError(f"analytic record {record['id']} does not match the active filter")

    evidence_hashes = sorted(
        (_normalize_semantic_sets(item) for item in analysis_bundle.get("evidence_hashes", [])),
        key=_canonical,
    )
    evidence_ids = sorted(
        {
            str(evidence_id)
            for record in claims + correlations
            for evidence_id in record["evidence_ids"]
        }
    )
    warnings = sorted(
        {
            str(warning)
            for warning in [
                *claims_core.get("warnings", []),
                *(gap.get("reason", "") for gap in analysis_bundle.get("source_gaps", []) if isinstance(gap, Mapping)),
            ]
            if warning
        }
    )
    analysis_core = _normalize_semantic_sets(dict(analysis_bundle))
    analysis_core["domains"] = domains
    for key in ("source_status", "source_gaps", "transformations", "evidence_hashes"):
        if isinstance(analysis_core.get(key), list):
            analysis_core[key] = sorted(analysis_core[key], key=_canonical)
    analysis_hash = hashlib.sha256(_canonical(analysis_core)).hexdigest()
    normalized_coverage = sorted(
        (_normalize_semantic_sets(item) for item in claims_core.get("coverage", [])),
        key=_canonical,
    )
    normalized_claims_core = {
        "record_type": claims_core.get("record_type"),
        "schema_version": claims_core.get("schema_version"),
        "transformation_version": claims_core.get("transformation_version"),
        "scope": _normalize_semantic_sets(claims_core["scope"]),
        "claims": claims,
        "correlations": correlations,
        "coverage": normalized_coverage,
        "warnings": sorted(set(claims_core.get("warnings", []))),
    }
    claims_hash = hashlib.sha256(_canonical(normalized_claims_core)).hexdigest()
    core = {
        "record_type": "study_data_export",
        "schema_version": SCHEMA_VERSION,
        "contract": "ael.study.data-export",
        "transformation_version": str(analysis_bundle.get("transformation_version", "0.3.0")),
        "schemas": {
            "analysis": "ael.study.analysis@0.3",
            "claims": "ael.study.claims@0.3",
            "export": "ael.study.data-export@0.3",
        },
        "active_filter": normalized_filter,
        "scope": _normalize_semantic_sets(claims_core["scope"]),
        "analysis_hash": analysis_hash,
        "claims_hash": claims_hash,
        "domains": domains,
        "claims": claims,
        "correlations": correlations,
        "coverage": normalized_coverage,
        "warnings": warnings,
        "provenance": {
            "evidence_ids": evidence_ids,
            "evidence_hashes": evidence_hashes,
        },
    }
    _safe_nested(core, "study_data_export")
    return {**core, "content_hash": hashlib.sha256(_canonical(core)).hexdigest()}


def encode_study_json(export: Mapping[str, Any]) -> bytes:
    """Encode a built export as byte-stable canonical JSON."""
    content_hash = export.get("content_hash")
    core = {key: value for key, value in export.items() if key != "content_hash"}
    if hashlib.sha256(_canonical(core)).hexdigest() != content_hash:
        raise ValueError("study JSON content hash verification failed")
    _safe_nested(export, "study_data_export")
    return _canonical(export) + b"\n"


__all__ = ["build_study_json", "encode_study_json", "normalize_study_filter"]
