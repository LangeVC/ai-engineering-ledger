from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path

from ael.ledger import OperationalLedger
from ael.study_view import _analysis_from_ledger, dashboard_view_from_canonical
from export.claims import generate_claim_registry
from export.study import StudyExporter
from export.study_json import build_study_json, encode_study_json, normalize_study_filter


START = "2026-10-01"
END_EXCLUSIVE = "2026-11-01"
TIMEZONE = "Europe/Berlin"


def _ingest(
    ledger: OperationalLedger,
    fixture_dir: Path,
    *,
    source: str,
    account: str,
    metric: str,
    value: str | int,
    unit: str,
    observed_at: str,
    aggregation: str = "interval_total",
    confidence: str = "1",
) -> None:
    path = fixture_dir / f"{source}-{account}-{metric}-{observed_at[:10]}.json"
    path.write_text(
        json.dumps(
            [
                {
                    "source": source,
                    "source_account_id": account,
                    "metric": metric,
                    "value": value,
                    "unit": unit,
                    "aggregation": aggregation,
                    "observed_at": observed_at,
                    "provenance": {
                        "class": "P1",
                        "method": "qa_replay_fixture",
                        "confidence": confidence,
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    receipt = ledger.ingest_file(
        source=source,
        account=account,
        kind="canonical",
        input_path=path,
    )
    assert receipt["accepted"] == 1


def _ledger_fixture(tmp_path: Path) -> OperationalLedger:
    ledger = OperationalLedger(tmp_path / "ledger.db")
    observations = (
        ("claude", "claude-pro", "input_tokens", 100, "token", "2026-10-02T12:00:00Z", "interval_total"),
        ("claude", "claude-pro", "input_tokens", 50, "token", "2026-10-03T12:00:00Z", "interval_total"),
        ("byteplus", "byteplus-01", "settlement_amount", "12.50", "USD", "2026-10-04T12:00:00Z", "interval_total"),
        ("github", "github-langevc", "commit_count", 7, "commit", "2026-10-05T12:00:00Z", "interval_total"),
        ("codex", "codex-plus", "quota_remaining", 80, "percent", "2026-10-06T12:00:00Z", "gauge"),
        ("codex", "codex-plus", "quota_remaining", 60, "percent", "2026-10-07T12:00:00Z", "gauge"),
    )
    for source, account, metric, value, unit, observed_at, aggregation in observations:
        _ingest(
            ledger,
            tmp_path,
            source=source,
            account=account,
            metric=metric,
            value=value,
            unit=unit,
            observed_at=observed_at,
            aggregation=aggregation,
        )
    with sqlite3.connect(ledger.database) as connection:
        connection.execute(
            "UPDATE import_receipts SET incomplete = 1 WHERE source = ? AND account = ?",
            ("claude", "claude-pro"),
        )
    return ledger


def _canonical_replay(ledger: OperationalLedger) -> tuple[dict, dict, dict, dict]:
    analysis, claim_specs, active_filter = _analysis_from_ledger(
        ledger.database,
        start=START,
        end_exclusive=END_EXCLUSIVE,
        timezone_name=TIMEZONE,
    )
    claims = generate_claim_registry(
        analysis,
        claim_specs=claim_specs,
        correlation_specs=[],
    )
    study = build_study_json(
        analysis_bundle=analysis,
        claims_bundle=claims,
        active_filter=active_filter,
    )
    dashboard = dashboard_view_from_canonical(
        analysis_bundle=analysis,
        claims_bundle=claims,
        active_filter=active_filter,
    )
    return analysis, claims, study, dashboard


def _direct_sql_total(
    ledger: OperationalLedger,
    where: dict[str, str],
    *,
    start: str,
    end_exclusive: str,
) -> Decimal:
    ordered_fields = ("source", "account", "metric", "unit", "scope", "aggregation")
    query = (
        "SELECT value_text FROM observations WHERE observed_at >= ? AND observed_at < ? "
        + "".join(f"AND {field} = ? " for field in ordered_fields)
        + "ORDER BY observed_at, observation_id"
    )
    params = [start, end_exclusive, *(where[field] for field in ordered_fields)]
    with sqlite3.connect(ledger.database) as connection:
        values = [Decimal(row[0]) for row in connection.execute(query, params)]
    assert values
    return values[-1] if where["aggregation"] == "gauge" else sum(values, Decimal("0"))


def test_local_replay_is_hash_stable_and_all_headline_totals_cross_check(tmp_path: Path) -> None:
    ledger = _ledger_fixture(tmp_path)
    first_analysis, first_claims, first_study, first_dashboard = _canonical_replay(ledger)
    second_analysis, second_claims, second_study, second_dashboard = _canonical_replay(ledger)

    assert first_analysis == second_analysis
    assert first_claims == second_claims
    assert encode_study_json(first_study) == encode_study_json(second_study)
    assert first_study["content_hash"] == second_study["content_hash"]
    assert first_dashboard["content_hash"] == first_study["content_hash"]
    assert second_dashboard["content_hash"] == second_study["content_hash"]

    normalized = normalize_study_filter({"start": START, "end_exclusive": END_EXCLUSIVE}, TIMEZONE)
    for claim in first_study["claims"]:
        where = claim["scope"]["dimensions"]["where"]
        direct = _direct_sql_total(
            ledger,
            where,
            start=normalized["start"],
            end_exclusive=normalized["end_exclusive"],
        )
        assert Decimal(str(claim["value"])) == direct, claim["id"]


def test_known_gap_is_consistent_in_manifest_dashboard_and_export(tmp_path: Path) -> None:
    ledger = _ledger_fixture(tmp_path)
    analysis, _, study, dashboard = _canonical_replay(ledger)
    assert analysis["source_gaps"]
    gap = analysis["source_gaps"][0]
    reason = gap["reason"]

    destination = tmp_path / "evidence-pack"
    StudyExporter(transformation_version="0.3.0").write(
        destination,
        {},
        filters={"from": START, "to": END_EXCLUSIVE},
        timezone_name=TIMEZONE,
        study_cutoff="2026-11-01T00:00:00Z",
        data_state=analysis["data_state"],
        source_status=[
            {
                "source_alias": gap["source"],
                "account_alias": gap["account"],
                "status": "partial",
            }
        ],
        missing_periods=[gap],
        evidence_hashes=analysis["evidence_hashes"],
        export_timestamp="2026-11-01T00:00:00Z",
    )
    manifest = json.loads(
        (destination / "analysis-manifest.json").read_text(encoding="utf-8")
    )

    assert gap in manifest["source_gaps"]
    assert reason in dashboard["notices"]
    assert reason in study["warnings"]


def test_evidence_pack_replays_twice_with_identical_content_hash(tmp_path: Path) -> None:
    ledger = _ledger_fixture(tmp_path)
    analysis, _, study, _ = _canonical_replay(ledger)
    canonical_rows = {
        name: domain["records"]
        for name, domain in study["domains"].items()
        if domain["records"]
    }
    kwargs = {
        "filters": {"from": START, "to": END_EXCLUSIVE},
        "timezone_name": TIMEZONE,
        "study_cutoff": "2026-11-01T00:00:00Z",
        "data_state": analysis["data_state"],
        "source_status": [
            {"source_alias": "claude", "account_alias": "claude-pro", "status": "partial"}
        ],
        "missing_periods": analysis["source_gaps"],
        "evidence_hashes": analysis["evidence_hashes"],
        "export_timestamp": "2026-11-01T00:00:00Z",
    }
    exporter = StudyExporter(transformation_version="0.3.0")
    # Feed the actual canonical records into the pack writer. Empty domains are
    # intentionally omitted so their unavailable state remains explicit rather
    # than being fabricated as a complete zero-record source.
    first_manifest = exporter.write(tmp_path / "first-pack", canonical_rows, **kwargs)
    second_manifest = exporter.write(tmp_path / "second-pack", canonical_rows, **kwargs)
    first_analysis_manifest = json.loads(
        (tmp_path / "first-pack" / "analysis-manifest.json").read_text(encoding="utf-8")
    )
    second_analysis_manifest = json.loads(
        (tmp_path / "second-pack" / "analysis-manifest.json").read_text(encoding="utf-8")
    )
    first_analysis = json.loads(
        (tmp_path / "first-pack" / "analysis.json").read_text(encoding="utf-8")
    )
    second_analysis = json.loads(
        (tmp_path / "second-pack" / "analysis.json").read_text(encoding="utf-8")
    )

    assert first_manifest["content_hash"] == second_manifest["content_hash"]
    assert first_analysis_manifest["content_hash"] == second_analysis_manifest["content_hash"]
    assert first_analysis == second_analysis
    assert (tmp_path / "first-pack" / "analysis.json").read_bytes() == (
        tmp_path / "second-pack" / "analysis.json"
    ).read_bytes()
    expected_count = sum(len(records) for records in canonical_rows.values())
    first_count = sum(
        len(first_analysis["domains"][name]["records"])
        for name in canonical_rows
    )
    assert first_count == expected_count == 6
    for name, records in canonical_rows.items():
        assert first_analysis["domains"][name]["records"] == records
        assert second_analysis["domains"][name]["records"] == records
