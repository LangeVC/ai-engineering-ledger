from __future__ import annotations

import sqlite3
from pathlib import Path

from collectors.faigate.adapter import FaigateAdapter, FaigateConfig, extract_correlation_ids


def _database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE requests (
        id INTEGER PRIMARY KEY, timestamp REAL, provider TEXT, model TEXT, layer TEXT, rule_name TEXT,
        prompt_tok INTEGER, compl_tok INTEGER, cache_hit INTEGER, cache_miss INTEGER, cost_usd REAL,
        latency_ms REAL, success INTEGER, error TEXT, requested_model TEXT, modality TEXT,
        client_profile TEXT, client_tag TEXT, decision_reason TEXT, confidence REAL,
        canonical_model TEXT, lane_family TEXT, route_type TEXT, lane_cluster TEXT,
        selection_path TEXT, runtime_window_state TEXT, recovered_recently INTEGER,
        last_recovered_issue_type TEXT, decision_details TEXT, attempt_order TEXT, route_summary TEXT
        )"""
    )
    connection.execute(
        "INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (1, 1790000000.0, "deepseek", "model", "gateway", "rule", 10, 2, 0, 0, 0.1,
         50.0, 1, "", "model", "chat", "test", "ses_explicit_123", "", 1.0, "model",
         "", "", "", "", "", 0, "", "{}", "[]", '{"task_id":"task_explicit_123"}'),
    )
    connection.commit()
    connection.close()


def test_dry_run_is_idempotent_execution_truth_only(tmp_path: Path) -> None:
    path = tmp_path / "faigate.db"
    _database(path)
    adapter = FaigateAdapter(FaigateConfig(db_path=str(path)))
    first = adapter.dry_run()
    second = adapter.dry_run()

    assert first["writes"] == 0
    assert first["records"] == 1
    assert first["idempotency_keys"] == second["idempotency_keys"]
    assert first["truth_semantics"] == "execution_observation"
    assert first["billing_truth"] is False
    assert first["capability"]["correlation_policy"] == "explicit_ids_only"


def test_only_explicit_string_correlation_ids_are_preserved() -> None:
    ids = extract_correlation_ids("generic", {"session_id": None, "project_id": 123, "task_id": "task_real"})
    assert ids == (None, None, None, "task_real")
