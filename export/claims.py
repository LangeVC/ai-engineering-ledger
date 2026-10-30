"""Deterministic claims and non-causal correlation registry for AEL v0.3."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo


SCHEMA_VERSION = "0.3"
FORMULA_VERSION = "1"
NON_VALUE_STATES = {"unknown", "unsupported", "unverified", "unavailable"}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _stable_id(prefix: str, value: Any) -> str:
    return f"{prefix}-{hashlib.sha256(_canonical(value)).hexdigest()[:20]}"


def _date_time(value: str, timezone_name: str) -> str:
    if "T" in value:
        return value
    return datetime.combine(datetime.fromisoformat(value).date(), time.min, ZoneInfo(timezone_name)).isoformat()


def _canonical_dimension(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonical_dimension(item) for key, item in value.items()}
    if isinstance(value, list):
        normalized = [_canonical_dimension(item) for item in value]
        unique = {_canonical(item): item for item in normalized}
        return [unique[key] for key in sorted(unique)]
    return value


def _scope(bundle: dict[str, Any], dimensions: dict[str, Any]) -> dict[str, Any]:
    filters = bundle["filters"]
    timezone_name = bundle["timezone"]
    filter_dimensions = {
        key: _canonical_dimension(value)
        for key, value in filters.items()
        if key not in {"start", "from", "end_exclusive", "to", "semantics", "interval", "timezone"}
    }
    bound_dimensions = {"filter": filter_dimensions, **dimensions}
    scope_seed = {
        "data_state": bundle.get("data_state", {}).get("id", "unknown"),
        "filters": filters,
        "dimensions": bound_dimensions,
    }
    return {
        "scope_id": _stable_id("scope", scope_seed),
        "scope_version": 1,
        "interval": {
            "start": _date_time(filters["start"], timezone_name),
            "end_exclusive": _date_time(filters["end_exclusive"], timezone_name),
            "timezone": timezone_name,
            "semantics": "[start,end_exclusive)",
        },
        "dimensions": bound_dimensions,
    }


def _domain(bundle: dict[str, Any], name: str) -> dict[str, Any]:
    try:
        domain = bundle["domains"][name]
    except KeyError as exc:
        raise ValueError(f"analysis bundle has no domain {name}") from exc
    if not isinstance(domain, dict) or not isinstance(domain.get("records"), list):
        raise ValueError(f"analysis domain {name} is malformed")
    return domain


def _evidence_ids(bundle: dict[str, Any], records: list[dict[str, Any]]) -> list[str]:
    evidence: set[str] = set()
    for record in records:
        values = record.get("evidence_ids", [])
        if isinstance(values, list):
            evidence.update(str(value) for value in values if value)
        if record.get("evidence_id"):
            evidence.add(str(record["evidence_id"]))
    if not evidence:
        evidence.update(
            str(item["evidence_id"])
            for item in bundle.get("evidence_hashes", [])
            if isinstance(item, dict) and item.get("evidence_id")
        )
    if not evidence:
        raise ValueError("claim or correlation has no evidence IDs")
    return sorted(evidence)


def _coverage(
    bundle: dict[str, Any],
    domain_statuses: list[str],
    records: list[dict[str, Any]],
    *,
    sample_count: int | None = None,
) -> dict[str, Any]:
    record_states = {record.get("state") for record in records if record.get("state")}
    statuses = set(domain_statuses) | record_states
    for status in ("unavailable", "unsupported", "unknown", "unverified", "partial"):
        if status in statuses:
            selected = status
            break
    else:
        selected = "complete"
    limitations = sorted(
        {
            warning
            for record in records
            for warning in record.get("warnings", [])
            if isinstance(warning, str) and warning
        }
    )
    if selected != "complete" and not limitations:
        limitations.append(f"coverage is {selected}")
    warning_codes = [] if selected == "complete" else [f"{selected.upper()}_COVERAGE"]
    scope = _scope(bundle, {})["interval"]
    return {
        "status": selected,
        "observed_from": scope["start"] if records else None,
        "observed_to_exclusive": scope["end_exclusive"] if records else None,
        "sample_count": len(records) if sample_count is None else sample_count,
        "limitations": limitations,
        "warning_codes": warning_codes,
    }


def _confidence(coverage: dict[str, Any]) -> dict[str, str]:
    level = {
        "complete": "high",
        "partial": "medium",
        "unverified": "low",
        "unknown": "unknown",
        "unsupported": "unknown",
        "unavailable": "unknown",
    }[coverage["status"]]
    return {"level": level, "method": "coverage-derived confidence v1"}


def _usable(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [record for record in records if record.get("state", "value") not in NON_VALUE_STATES]


def _units(records: list[dict[str, Any]], field: str = "unit") -> set[str]:
    return {str(record[field]) for record in records if record.get(field)}


def _sum(records: list[dict[str, Any]], field: str) -> str:
    total = Decimal("0")
    for record in records:
        try:
            total += Decimal(str(record[field]))
        except (InvalidOperation, KeyError) as exc:
            raise ValueError(f"non-numeric {field} in claim input") from exc
    return format(total, "f")


def _reject_allowance_conversion(input_units: set[str], output_unit: str) -> None:
    token_input = any("token" in unit.lower() for unit in input_units)
    allowance_output = any(term in output_unit.lower() for term in ("allowance", "credit", "quota"))
    if token_input and allowance_output:
        raise ValueError("raw token activity cannot be converted to subscription allowance, credits, or quota")


def _registry_id(operation: str, version: str) -> str:
    normalized = re.sub(r"[^a-z0-9._-]+", "-", operation.lower()).strip("-")
    return f"{normalized}.v{version}"


def _formula(
    *,
    operation: str,
    version: str,
    expression: str,
    operand_units: set[str],
    output_unit: str,
) -> dict[str, Any]:
    declared_operands = sorted(operand_units) or [output_unit]
    return {
        "version": version,
        "registry_id": _registry_id(operation, version),
        "expression": expression,
        "unit_contract": {
            "operand_units": declared_operands,
            "output_unit": output_unit,
            "compatible": True,
        },
    }


def _value_state(value: Any, coverage_status: str) -> str:
    if coverage_status in NON_VALUE_STATES:
        return coverage_status
    if coverage_status == "partial":
        return "partial"
    if value is not None:
        try:
            if Decimal(str(value)) == 0:
                return "evidenced_zero"
        except InvalidOperation:
            pass
    return "observed"


def _claim(bundle: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    name = str(spec["name"])
    if spec.get("causality_assertion") not in (None, False):
        raise ValueError("claims must not assert causality")
    if spec.get("provider_attribution") not in (None, False):
        raise ValueError("claims must not assert provider attribution")
    domain = _domain(bundle, str(spec["domain"]))
    where = dict(spec.get("where", {}))
    records = [
        record
        for record in _usable(domain["records"])
        if all(record.get(key) == value for key, value in where.items())
    ]
    units = _units(records)
    if len(units) > 1:
        raise ValueError(f"claim {name} cannot sum incompatible units: {sorted(units)}")
    unit = str(spec.get("unit") or (next(iter(units)) if units else "unavailable"))
    _reject_allowance_conversion(units, unit)
    if units and unit not in units:
        raise ValueError(f"claim {name} output unit does not match input unit")

    operation = spec.get("operation", "sum")
    field = str(spec.get("field", "value"))
    if operation == "sum":
        value: Any = _sum(records, field) if records else None
        expression = f"sum({spec['domain']}.{field})"
    elif operation == "count":
        value = len(records)
        expression = f"count({spec['domain']})"
    elif operation == "latest":
        ordered = sorted(
            records,
            key=lambda record: (str(record.get("observed_at", "")), _canonical(record)),
        )
        value = ordered[-1].get(field) if ordered else None
        expression = f"latest({spec['domain']}.{field}) by observed_at"
    else:
        raise ValueError(f"unsupported claim operation {operation}")

    dimensions = {
        "domain": spec["domain"],
        "where": where,
        **dict(spec.get("dimensions", {})),
    }
    coverage = _coverage(bundle, [domain["status"]], records)
    if coverage["status"] in NON_VALUE_STATES:
        value = None
    warnings = list(coverage["limitations"])
    seed = {"name": name, "formula": expression, "scope": _scope(bundle, dimensions)}
    version = str(spec.get("formula_version", FORMULA_VERSION))
    return {
        "record_type": "claim",
        "schema_version": SCHEMA_VERSION,
        "id": _stable_id("claim", seed),
        "formula": _formula(
            operation=f"{operation}.same-unit",
            version=version,
            expression=expression,
            operand_units=units,
            output_unit=unit,
        ),
        "scope": seed["scope"],
        "evidence_ids": _evidence_ids(bundle, records),
        "coverage": coverage,
        "confidence": _confidence(coverage),
        "unit": unit,
        "value": value,
        "value_state": _value_state(value, coverage["status"]),
        "causality_assertion": False,
        "provider_attribution": False,
        "warnings": warnings,
    }


def _reject_provider_causality(left: str, right: str) -> None:
    provider_side = "provider" in left.lower() or "provider" in right.lower()
    output_side = any(
        term in f"{left} {right}".lower()
        for term in ("output", "commit", "release", "engineering", "productivity")
    )
    if provider_side and output_side:
        raise ValueError("period co-occurrence cannot attribute engineering output to a provider")


def _pearson(pairs: list[tuple[Decimal, Decimal]]) -> str | None:
    if len(pairs) < 2:
        return None
    left = [float(pair[0]) for pair in pairs]
    right = [float(pair[1]) for pair in pairs]
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right, strict=True))
    denominator = math.sqrt(
        sum((x - left_mean) ** 2 for x in left) * sum((y - right_mean) ** 2 for y in right)
    )
    return None if denominator == 0 else format(numerator / denominator, ".12g")


def _correlation(bundle: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    left_name = str(spec["left_domain"])
    right_name = str(spec["right_domain"])
    left_dimension = str(spec.get("left_dimension", left_name))
    right_dimension = str(spec.get("right_dimension", right_name))
    _reject_provider_causality(left_dimension, right_dimension)
    if spec.get("causality_assertion") not in (None, False):
        raise ValueError("correlations must not assert causality")

    left_domain = _domain(bundle, left_name)
    right_domain = _domain(bundle, right_name)
    left_records = _usable(left_domain["records"])
    right_records = _usable(right_domain["records"])
    join_key = str(spec.get("join_key", "period"))
    left_field = str(spec.get("left_field", "value"))
    right_field = str(spec.get("right_field", "value"))
    left_units = _units(left_records)
    right_units = _units(right_records)
    if len(left_units) > 1 or len(right_units) > 1:
        raise ValueError("correlation dimensions contain incompatible units")

    left_by_key = {record[join_key]: record for record in left_records if join_key in record}
    right_by_key = {record[join_key]: record for record in right_records if join_key in record}
    shared = sorted(set(left_by_key) & set(right_by_key), key=str)
    pairs: list[tuple[Decimal, Decimal]] = []
    paired_records: list[dict[str, Any]] = []
    for key in shared:
        try:
            pair = (Decimal(str(left_by_key[key][left_field])), Decimal(str(right_by_key[key][right_field])))
        except (InvalidOperation, KeyError) as exc:
            raise ValueError("correlation inputs must be numeric") from exc
        pairs.append(pair)
        paired_records.extend((left_by_key[key], right_by_key[key]))

    value = _pearson(pairs)
    dimensions = {
        "left_domain": left_name,
        "right_domain": right_name,
        "join_key": join_key,
    }
    coverage = _coverage(
        bundle,
        [left_domain["status"], right_domain["status"]],
        paired_records,
        sample_count=len(pairs),
    )
    warnings = list(coverage["limitations"])
    warnings.append("Observed correlation is non-causal and must not be interpreted as provider attribution.")
    if value is None:
        warnings.append("Correlation is unavailable because fewer than two varying paired observations exist.")
    expression = f"pearson({left_name}.{left_field},{right_name}.{right_field}) by {join_key}"
    seed = {"name": spec["name"], "formula": expression, "scope": _scope(bundle, dimensions)}
    version = str(spec.get("formula_version", FORMULA_VERSION))
    return {
        "record_type": "correlation",
        "schema_version": SCHEMA_VERSION,
        "id": _stable_id("correlation", seed),
        "formula": _formula(
            operation="pearson.dimensionless",
            version=version,
            expression=expression,
            operand_units=left_units | right_units,
            output_unit="pearson_r",
        ),
        "scope": seed["scope"],
        "evidence_ids": _evidence_ids(bundle, paired_records),
        "coverage": coverage,
        "confidence": _confidence(coverage),
        "unit": "pearson_r",
        "value": value,
        "value_state": _value_state(value, coverage["status"]),
        "warnings": sorted(set(warnings)),
        "left_dimension": left_dimension,
        "right_dimension": right_dimension,
        "sample_size": len(pairs),
        "causality_assertion": False,
    }


def generate_claim_registry(
    analysis_bundle: dict[str, Any],
    *,
    claim_specs: list[dict[str, Any]],
    correlation_specs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return a deterministic study bundle containing claims and correlations."""
    if analysis_bundle.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("claims engine requires ael.study.analysis schema version 0.3")
    claims = sorted((_claim(analysis_bundle, spec) for spec in claim_specs), key=lambda item: item["id"])
    correlations = sorted(
        (_correlation(analysis_bundle, spec) for spec in correlation_specs), key=lambda item: item["id"]
    )
    scope = _scope(analysis_bundle, {"registry": "claims-and-correlations"})
    coverage = sorted(
        [item["coverage"] for item in claims + correlations],
        key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
    )
    warnings = sorted({warning for item in claims + correlations for warning in item["warnings"]})
    core = {
        "record_type": "study_bundle",
        "schema_version": SCHEMA_VERSION,
        "transformation_version": "0.3.0",
        "scope": scope,
        "claims": claims,
        "correlations": correlations,
        "coverage": coverage,
        "warnings": warnings,
    }
    return {**core, "content_hash": hashlib.sha256(_canonical(core)).hexdigest()}


__all__ = ["generate_claim_registry"]
