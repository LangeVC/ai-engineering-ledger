"""Deterministic, operator-triggered local dashboard snapshot specifications."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any


_HASH = re.compile(r"^[0-9a-f]{64}$")
_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_PRIVATE_PATH = re.compile(r"(?:^|[\s\"'])(?:/Users/|/home/|[A-Za-z]:\\Users\\)")
_PRIVATE_REMOTE = re.compile(
    r"(?i)(?:\bgit@[^\s:]+:[^\s]+|\bssh://\S+|\bhttps?://[^\s]+\.git(?:\b|$)|"
    r"\bhttps?://[^\s/@:]+:[^\s/@]+@|\bhttps?://(?:localhost|127\.0\.0\.1|[^\s/]+\.(?:local|internal))\b)"
)
_EMAIL = re.compile(
    r"(?i)(?<![A-Z0-9._%+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![A-Z0-9._%+-])"
)
_SECRET = re.compile(
    r"(?:\bBearer(?:\s+|[-_:])[A-Za-z0-9._~-]{6,}|\bsk-[A-Za-z0-9_-]{8,}|\bapi[_-]?key\s*[:=])",
    re.IGNORECASE,
)
_DIMENSIONS = ("providers", "accounts", "products", "repositories", "models", "capabilities")
_NON_VALUE_COVERAGE = {"unknown", "unsupported", "unverified", "unavailable"}


def _utc(value: str) -> str:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("dashboard filter timestamps must be timezone-aware")
    return parsed.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _scan_private(value: Any) -> None:
    if isinstance(value, str):
        if _PRIVATE_PATH.search(value):
            raise ValueError("dashboard snapshot contains a private absolute path")
        if _PRIVATE_REMOTE.search(value):
            raise ValueError("dashboard snapshot contains a private remote")
        if _EMAIL.search(value):
            raise ValueError("dashboard snapshot contains an email address")
        if _SECRET.search(value):
            raise ValueError("dashboard snapshot contains a secret-like value")
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _scan_private(str(key))
            _scan_private(item)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for item in value:
            _scan_private(item)


def _active_filter(filter_spec: Mapping[str, Any], approved_identifiers: set[str]) -> dict[str, Any]:
    required = {"start", "end_exclusive", *_DIMENSIONS}
    missing = required - set(filter_spec)
    if missing:
        raise ValueError(f"active filter omits dimensions: {', '.join(sorted(missing))}")
    unexpected = set(filter_spec) - required
    if unexpected:
        raise ValueError(f"active filter has unsupported fields: {', '.join(sorted(unexpected))}")
    start = _utc(str(filter_spec["start"]))
    end = _utc(str(filter_spec["end_exclusive"]))
    if start >= end:
        raise ValueError("dashboard filter must be a non-empty half-open interval")

    dimensions: dict[str, list[str]] = {}
    for name in _DIMENSIONS:
        raw = filter_spec[name]
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
            raise ValueError(f"filter dimension {name} must be a list")
        values = [str(value) for value in raw]
        if len(set(values)) != len(values) or any(not _STABLE_ID.fullmatch(value) for value in values):
            raise ValueError(f"filter dimension {name} contains invalid identifiers")
        unapproved = set(values) - approved_identifiers
        if unapproved:
            raise ValueError(f"unapproved dashboard identifiers: {', '.join(sorted(unapproved))}")
        dimensions[name] = sorted(values)
    normalized = {
        "start": start,
        "end_exclusive": end,
        "boundary": "half_open",
        "dimensions": dimensions,
    }
    _scan_private(normalized)
    return normalized


def _verified_bundle(
    bundle: Mapping[str, Any], dataset_hash: str
) -> tuple[list[Mapping[str, Any]], str]:
    if bundle.get("dataset_hash") != dataset_hash:
        raise ValueError("claim bundle is not bound to the selected dataset hash")
    content_hash = bundle.get("content_hash")
    if not isinstance(content_hash, str) or not _HASH.fullmatch(content_hash):
        raise ValueError("claim bundle has no valid content hash")
    hash_core = {key: value for key, value in bundle.items() if key not in {"content_hash", "dataset_hash"}}
    if hashlib.sha256(_canonical(hash_core)).hexdigest() != content_hash:
        raise ValueError("claim bundle content hash verification failed")
    records = [*bundle.get("claims", []), *bundle.get("correlations", [])]
    if any(not isinstance(record, Mapping) for record in records):
        raise ValueError("claim bundle records must be objects")
    # The producer contract treats claim and correlation collections as sets.
    # Derive the binding hash from their canonical order so harmless array
    # permutations do not change an otherwise identical snapshot.
    normalized_core = dict(hash_core)
    normalized_core["claims"] = sorted(
        bundle.get("claims", []), key=lambda record: str(record.get("id", ""))
    )
    normalized_core["correlations"] = sorted(
        bundle.get("correlations", []), key=lambda record: str(record.get("id", ""))
    )
    semantic_hash = hashlib.sha256(_canonical(normalized_core)).hexdigest()
    return records, semantic_hash


def _numeric_value(record: Mapping[str, Any]) -> int | float | str | None:
    value = record.get("value")
    coverage = record.get("coverage")
    if not isinstance(coverage, Mapping) or not isinstance(coverage.get("status"), str):
        raise ValueError("claim record requires typed coverage")
    if value is None:
        if coverage["status"] not in _NON_VALUE_COVERAGE:
            raise ValueError("claim value may be null only for non-value coverage states")
        return None
    if isinstance(value, bool) or isinstance(value, (dict, list)):
        raise ValueError("claim value must be numeric or an explicit null state")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("claim value must be numeric or an explicit null state") from exc
    if not parsed.is_finite() or (isinstance(value, float) and not math.isfinite(value)):
        raise ValueError("claim value must be finite")
    return value


def _selected_claims(
    records: Sequence[Mapping[str, Any]], claim_ids: Sequence[str]
) -> list[dict[str, Any]]:
    stable_ids = sorted(set(claim_ids))
    if not stable_ids or len(stable_ids) != len(claim_ids):
        raise ValueError("claim_ids must be non-empty and unique")
    by_id: dict[str, Mapping[str, Any]] = {}
    for record in records:
        record_id = record.get("id")
        if not isinstance(record_id, str) or not _STABLE_ID.fullmatch(record_id):
            raise ValueError("claim bundle contains an unstable record id")
        if record_id in by_id:
            raise ValueError("claim bundle contains duplicate record ids")
        by_id[record_id] = record
    missing = set(stable_ids) - set(by_id)
    if missing:
        raise ValueError(f"claim_ids not present in verified bundle: {', '.join(sorted(missing))}")

    selected: list[dict[str, Any]] = []
    for claim_id in stable_ids:
        record = by_id[claim_id]
        if record.get("record_type") not in {"claim", "correlation"}:
            raise ValueError("snapshot accepts only validated claim or correlation records")
        unit = record.get("unit")
        if not isinstance(unit, str) or not unit.strip():
            raise ValueError("claim record requires a typed unit")
        coverage = record["coverage"]
        limitations = coverage.get("limitations", [])
        if not isinstance(limitations, list) or any(not isinstance(item, str) for item in limitations):
            raise ValueError("claim coverage limitations must be strings")
        selected.append(
            {
                "claim_id": claim_id,
                "record_type": record["record_type"],
                "value": _numeric_value(record),
                "unit": unit,
                "coverage": {
                    "status": coverage["status"],
                    "sample_count": coverage.get("sample_count"),
                    "limitations": sorted(limitations),
                },
            }
        )
    return selected


def build_dashboard_snapshot(
    *,
    title: str,
    active_filter: Mapping[str, Any],
    dataset_hash: str,
    claims_bundle: Mapping[str, Any],
    claim_ids: Sequence[str],
    alt_text: str,
    approved_identifiers: Sequence[str] = (),
) -> dict[str, Any]:
    """Build an optional JSON snapshot solely from a verified claims bundle."""
    normalized_title = title.strip()
    normalized_alt = alt_text.strip()
    if not normalized_title or not normalized_alt:
        raise ValueError("title and alt_text are required")
    if not _HASH.fullmatch(dataset_hash):
        raise ValueError("dataset_hash must be a lowercase sha256 digest")
    approved = set(approved_identifiers)
    normalized_filter = _active_filter(active_filter, approved)
    filter_digest = hashlib.sha256(_canonical(normalized_filter)).hexdigest()
    records, claims_bundle_hash = _verified_bundle(claims_bundle, dataset_hash)
    plotted_claims = _selected_claims(records, claim_ids)
    coverage_note = "; ".join(
        f"{record['claim_id']}: {record['coverage']['status']}"
        + (
            f" ({', '.join(record['coverage']['limitations'])})"
            if record["coverage"]["limitations"]
            else ""
        )
        for record in plotted_claims
    )
    core: dict[str, Any] = {
        "schema_version": "dashboard-snapshot-v0.2",
        "trigger": "operator",
        "optional": True,
        "title": normalized_title,
        "active_filter": normalized_filter,
        "filter_digest": filter_digest,
        "dataset_hash": dataset_hash,
        "claims_bundle_hash": claims_bundle_hash,
        "claims": plotted_claims,
        "coverage_note": coverage_note,
        "alt_text": normalized_alt,
        "rendering": {"required": False, "format": "json-specification"},
    }
    _scan_private(core)
    content_hash = hashlib.sha256(_canonical(core)).hexdigest()
    return {
        **core,
        "snapshot_id": f"dashboard_snapshot_{content_hash[:24]}",
        "content_hash": content_hash,
    }


def encode_dashboard_snapshot(snapshot: Mapping[str, Any]) -> bytes:
    """Encode a previously built snapshot deterministically."""
    _scan_private(snapshot)
    return _canonical(snapshot) + b"\n"
