from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from ael.ledger import LIVING_SCHEMA_VERSION, OperationalLedger


SCHEMA_DIR = Path(__file__).parents[2] / "schema" / "living"


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("collection-job.schema.json", {"schema_version": "0.4", "job_id": "job_deepseek", "source_alias": "deepseek-main", "cadence": "daily", "enabled": True, "revision": 1}),
        ("source-health.schema.json", {"schema_version": "0.4", "source_alias": "deepseek-main", "state": "healthy", "reason_code": "within_cadence", "rule_version": "0.4.0", "evaluated_at": "2026-10-30T12:00:00Z"}),
        ("study-snapshot.schema.json", {"record_type": "study_snapshot", "schema_version": "0.4", "snapshot_id": "snap_" + "a" * 20, "status": "draft", "created_at": "2026-10-30T12:00:00Z", "filter": {"start": "2026-04-26", "end_exclusive": "2026-11-01"}, "transformation_version": "0.4.0"}),
        ("study-diff.schema.json", {"record_type": "study_diff", "schema_version": "0.4", "algorithm_version": "0.4.0", "from_snapshot_id": "snap_a", "to_snapshot_id": "snap_b", "changes": [], "content_hash": "a" * 64}),
        ("evidence-domain-record.schema.json", {"schema_version": "0.4", "record_id": "proc_1", "domain": "process", "interval": {"start": "2026-10-30T10:00:00Z", "end_exclusive": "2026-10-30T11:00:00Z"}, "confidence": 1, "coverage": "complete", "provenance": {"method": "fixture", "evidence_ids": ["sha256:a"]}, "payload": {}}),
    ],
)
def test_v040_contract_positive_fixtures(name: str, payload: dict[str, object]) -> None:
    schema = json.loads((SCHEMA_DIR / name).read_text())
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(payload)


@pytest.mark.parametrize("name", [path.name for path in SCHEMA_DIR.glob("*.schema.json")])
def test_v040_contracts_reject_empty_objects(name: str) -> None:
    schema = json.loads((SCHEMA_DIR / name).read_text())
    assert list(Draft202012Validator(schema).iter_errors({}))


def test_v040_migration_is_additive_and_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "ael.db"
    ledger = OperationalLedger(database)
    ledger.initialize()
    ledger.initialize()
    assert ledger.health()["schema_version"] == "0.2"
    with sqlite3.connect(database) as connection:
        version = connection.execute("SELECT value FROM metadata WHERE key='living_schema_version'").fetchone()[0]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert version == LIVING_SCHEMA_VERSION == "0.4"
    assert {"collection_jobs", "collection_runs", "source_health", "study_snapshots", "living_pointers", "evidence_domain_records"} <= tables


def test_packaged_contract_registry_keeps_v030_read_compatibility() -> None:
    registry_path = Path(__file__).parents[2] / "ael" / "resources" / "living-contracts-v0.4.json"
    registry = json.loads(registry_path.read_text())
    assert registry["schema_version"] == "0.4"
    assert "ael.study.data-export@0.3" in registry["compatibility"]["reads"]
    assert set(registry["contracts"]) == {
        "collection_job", "source_health", "study_snapshot", "study_diff", "evidence_domain_record"
    }
    assert all((registry_path.parent / filename).is_file() for filename in registry["contracts"].values())

