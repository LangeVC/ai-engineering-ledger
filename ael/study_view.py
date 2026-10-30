"""Canonical local dashboard projection backed by the v0.3 study-data contract."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from export.claims import generate_claim_registry
from export.study_json import build_study_json, normalize_study_filter


DOMAIN_NAMES = (
    "resources",
    "runtime",
    "engineering",
    "products",
    "costs",
    "coverage",
    "reconciliation",
    "provenance",
)
NON_COMPLETE = {"partial", "unknown", "unsupported", "unverified", "unavailable"}


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _family(source: str, metric: str, unit: str) -> tuple[str, str]:
    lowered = metric.lower()
    if source in {"github", "forgejo", "git"} or any(
        term in lowered for term in ("commit", "release", "pull_request", "repository", "loc", "line")
    ):
        return "engineering", "output"
    if unit.upper() in {"USD", "EUR", "GBP", "JPY", "CNY"} or any(
        term in lowered for term in ("cost", "amount", "fee", "spend", "cash_paid")
    ):
        return "costs", "costs"
    return "runtime", "usage"


def _available_filter(
    connection: sqlite3.Connection,
    start: str | None,
    end_exclusive: str | None,
    timezone_name: str,
) -> dict[str, str]:
    first, last = connection.execute("SELECT MIN(observed_at), MAX(observed_at) FROM observations").fetchone()
    zone = ZoneInfo(timezone_name)
    if first:
        first_date = datetime.fromisoformat(first.replace("Z", "+00:00")).astimezone(zone).date()
        last_date = datetime.fromisoformat(last.replace("Z", "+00:00")).astimezone(zone).date()
        default_start = first_date.isoformat()
        default_end = (last_date + timedelta(days=1)).isoformat()
    else:
        default_start = "1970-01-01"
        default_end = "1970-01-02"
    return {"start": start or default_start, "end_exclusive": end_exclusive or default_end}


def _analysis_from_ledger(
    database: Path,
    *,
    start: str | None,
    end_exclusive: str | None,
    timezone_name: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        requested_filter = _available_filter(connection, start, end_exclusive, timezone_name)
        normalized_filter = normalize_study_filter(requested_filter, timezone_name)
        rows = connection.execute(
            "SELECT * FROM observations WHERE observed_at >= ? AND observed_at < ? "
            "ORDER BY observed_at, observation_id",
            (normalized_filter["start"], normalized_filter["end_exclusive"]),
        ).fetchall()
        incomplete_sources = {
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT DISTINCT source, account FROM import_receipts WHERE incomplete = 1"
            ).fetchall()
        }

    domains: dict[str, dict[str, Any]] = {
        name: {"status": "unavailable", "records": []} for name in DOMAIN_NAMES
    }
    claim_specs: list[dict[str, Any]] = []
    evidence_hashes: dict[str, dict[str, str]] = {}
    source_gaps: list[dict[str, str]] = []
    source_status: list[dict[str, str]] = []
    groups: set[tuple[str, ...]] = set()

    for row in rows:
        domain_name, metric_family = _family(row["source"], row["metric"], row["unit"])
        incomplete = (row["source"], row["account"]) in incomplete_sources
        partial = Decimal(row["confidence"]) < 1 or incomplete
        warnings = []
        if Decimal(row["confidence"]) < 1:
            warnings.append("observation confidence is partial")
        if incomplete:
            warnings.append("source import reported incomplete coverage")
        record = {
            "source": row["source"],
            "account": row["account"],
            "metric": row["metric"],
            "metric_family": metric_family,
            "aggregation": row["aggregation"],
            "scope": row["scope"],
            "observed_at": row["observed_at"],
            "state": "partial" if partial else "value",
            "value": row["value_text"],
            "unit": row["unit"],
            "evidence_ids": [row["evidence_sha256"]],
            "warnings": warnings,
        }
        domains[domain_name]["records"].append(record)
        evidence_hashes[row["evidence_sha256"]] = {
            "evidence_id": row["evidence_sha256"],
            "sha256": row["evidence_sha256"],
        }
        groups.add(
            (
                domain_name,
                row["source"],
                row["account"],
                row["metric"],
                row["unit"],
                row["scope"],
                row["aggregation"],
                metric_family,
            )
        )

    for domain in domains.values():
        if domain["records"]:
            domain["records"].sort(key=_canonical)
            domain["status"] = (
                "partial" if any(record["state"] == "partial" for record in domain["records"]) else "complete"
            )
    # Import completeness is evidence about the source, not about whether an
    # observation happens to fall inside the active dashboard interval.  Keep
    # known gaps visible even for an otherwise empty bounded query.
    for source, account in sorted(incomplete_sources):
        reason = "source import reported incomplete coverage"
        source_status.append(
            {
                "source": source,
                "account": account,
                "status": "partial",
                "reason": reason,
            }
        )
        source_gaps.append(
            {
                "source": source,
                "account": account,
                "reason": reason,
            }
        )

    for domain_name, source, account, metric, unit, scope, aggregation, metric_family in sorted(groups):
        where = {
            "source": source,
            "account": account,
            "metric": metric,
            "unit": unit,
            "scope": scope,
            "aggregation": aggregation,
        }
        claim_specs.append(
            {
                "name": ":".join((source, account, metric, unit, scope, aggregation)),
                "domain": domain_name,
                "operation": "latest" if aggregation == "gauge" else "sum",
                "field": "value",
                "unit": unit,
                "where": where,
                "dimensions": {"metric_family": metric_family},
            }
        )

    dataset_seed = {
        "filter": normalized_filter,
        "evidence": sorted(evidence_hashes),
        "records": sum(len(domain["records"]) for domain in domains.values()),
    }
    analysis = {
        "schema_version": "0.3",
        "contract": "ael.study.analysis",
        "transformation_version": "0.3.0",
        "filters": requested_filter,
        "timezone": timezone_name,
        "analysis_cutoff": normalized_filter["end_exclusive"],
        "data_state": {
            "id": "ledger-" + hashlib.sha256(_canonical(dataset_seed)).hexdigest()[:20],
            "as_of": normalized_filter["end_exclusive"],
        },
        "source_status": source_status,
        "source_gaps": source_gaps,
        "transformations": ["operational-ledger-to-analysis@0.3"],
        "observation_phase_counts": {"reconstructed": 0, "prospective": len(rows), "unknown": 0},
        "evidence_hashes": sorted(evidence_hashes.values(), key=_canonical),
        "domains": domains,
    }
    return analysis, claim_specs, requested_filter


def dashboard_view_from_canonical(
    *,
    analysis_bundle: Mapping[str, Any],
    claims_bundle: Mapping[str, Any],
    active_filter: Mapping[str, Any],
) -> dict[str, Any]:
    """Project only verified canonical study JSON; never recompute numeric claims."""
    study = build_study_json(
        analysis_bundle=analysis_bundle,
        claims_bundle=claims_bundle,
        active_filter=active_filter,
    )
    notices = {str(warning) for warning in study["warnings"] if warning}
    for name, domain in study["domains"].items():
        if domain["status"] != "complete":
            notices.add(f"{name} coverage is {domain['status']}")
    for record in [*study["claims"], *study["correlations"]]:
        if record["coverage"]["status"] != "complete":
            notices.update(str(item) for item in record["coverage"].get("limitations", []) if item)

    groups = []
    families: dict[str, list[dict[str, Any]]] = {"costs": [], "usage": [], "output": []}
    for claim in study["claims"]:
        dimensions = claim["scope"].get("dimensions", {})
        where = dimensions.get("where", {})
        family = dimensions.get("metric_family", "usage")
        group = {
            "id": claim["id"],
            "source": where.get("source", "study"),
            "account": where.get("account", "study"),
            "metric": where.get("metric", claim["id"]),
            "unit": claim["unit"],
            "scope": where.get("scope", "study"),
            "aggregation": where.get("aggregation", "derived"),
            "value": claim["value"],
            "value_state": claim["value_state"],
            "observation_count": claim["coverage"].get("sample_count", 0),
            "window_type": None,
            "latest_reset_at": None,
            "confidence_distribution": {
                claim["confidence"].get("level", "unknown"): claim["coverage"].get(
                    "sample_count", 0
                )
            },
            "coverage": claim["coverage"],
            "confidence": claim["confidence"],
            "evidence_ids": claim["evidence_ids"],
        }
        groups.append(group)
        families.setdefault(family, []).append(group)
    groups.sort(key=lambda group: group["id"])
    for records in families.values():
        records.sort(key=lambda group: group["id"])
    return {
        "schema_version": "0.3",
        "contract": "ael.dashboard.study-view@0.3",
        "content_hash": study["content_hash"],
        "filters": study["active_filter"],
        "domains": study["domains"],
        "claims": study["claims"],
        "correlations": study["correlations"],
        "coverage": study["coverage"],
        "provenance": study["provenance"],
        "groups": groups,
        "metric_families": families,
        "notices": sorted(notices),
        "observation_count": sum(len(domain["records"]) for domain in study["domains"].values()),
    }


def build_ledger_dashboard_view(
    database: str | Path,
    *,
    start: str | None = None,
    end_exclusive: str | None = None,
    timezone_name: str = "Europe/Berlin",
) -> dict[str, Any]:
    database_path = Path(database)
    analysis, claim_specs, active_filter = _analysis_from_ledger(
        database_path,
        start=start,
        end_exclusive=end_exclusive,
        timezone_name=timezone_name,
    )
    claims = generate_claim_registry(
        analysis,
        claim_specs=claim_specs,
        correlation_specs=[],
    )
    view = dashboard_view_from_canonical(
        analysis_bundle=analysis,
        claims_bundle=claims,
        active_filter=active_filter,
    )
    view["available_range"] = {
        "start": analysis["filters"]["start"],
        "end_exclusive": analysis["filters"]["end_exclusive"],
        "timezone": analysis["timezone"],
    }
    return view


__all__ = ["build_ledger_dashboard_view", "dashboard_view_from_canonical"]
