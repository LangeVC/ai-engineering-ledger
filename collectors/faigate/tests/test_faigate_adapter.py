"""
Unit and Contract Tests for Faigate Collector Adapter.
Tests PRD US-10, PRD US-15, and schema/collector compliance.
"""

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone

from collectors.faigate.adapter import (
    FaigateAdapter,
    FaigateConfig,
    format_iso_timestamp,
    generate_idempotency_key,
    extract_correlation_ids,
)
from collectors.faigate.reconcile import calculate_reconciliation


def create_in_memory_db_with_schema() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.execute("""
    CREATE TABLE requests (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp   REAL    NOT NULL,
        provider    TEXT    NOT NULL,
        model       TEXT    NOT NULL,
        layer       TEXT    NOT NULL,
        rule_name   TEXT    NOT NULL,
        prompt_tok  INTEGER DEFAULT 0,
        compl_tok   INTEGER DEFAULT 0,
        cache_hit   INTEGER DEFAULT 0,
        cache_miss  INTEGER DEFAULT 0,
        cost_usd    REAL    DEFAULT 0,
        latency_ms  REAL    DEFAULT 0,
        success     INTEGER DEFAULT 1,
        error       TEXT    DEFAULT '',
        requested_model TEXT DEFAULT 'auto',
        modality        TEXT DEFAULT 'chat',
        client_profile  TEXT DEFAULT 'generic',
        client_tag      TEXT DEFAULT '',
        decision_reason TEXT DEFAULT '',
        confidence      REAL DEFAULT 0,
        canonical_model TEXT DEFAULT '',
        lane_family     TEXT DEFAULT '',
        route_type      TEXT DEFAULT '',
        lane_cluster    TEXT DEFAULT '',
        selection_path  TEXT DEFAULT '',
        runtime_window_state TEXT DEFAULT '',
        recovered_recently INTEGER DEFAULT 0,
        last_recovered_issue_type TEXT DEFAULT '',
        decision_details TEXT DEFAULT '{}',
        attempt_order   TEXT DEFAULT '[]',
        route_summary TEXT DEFAULT '{}'
    )
    """)
    return con


class TestFaigateAdapter(unittest.TestCase):

    def setUp(self):
        self.fixtures_dir = os.path.join(os.path.dirname(__file__), "fixtures")

    def test_format_iso_timestamp(self):
        ts = 1792628000.0
        iso = format_iso_timestamp(ts)
        self.assertTrue(iso.endswith("Z"))
        self.assertIn("T", iso)

    def test_extract_correlation_ids_preserves_identities_without_hallucination(self):
        # US-10.d: Preserve existing IDs, do not invent
        tag = "kilo-build ses_0123456789 proj_abc123"
        route_summary = {"council_id": "council_xyz"}
        ses, prj, cnl, tsk = extract_correlation_ids(tag, route_summary)
        self.assertEqual(ses, "ses_0123456789")
        self.assertEqual(prj, "proj_abc123")
        self.assertEqual(cnl, "council_xyz")
        self.assertIsNone(tsk)  # No task ID invented

        # Non-matching tag remains None
        ses2, prj2, cnl2, tsk2 = extract_correlation_ids("generic", {})
        self.assertIsNone(ses2)
        self.assertIsNone(prj2)
        self.assertIsNone(cnl2)
        self.assertIsNone(tsk2)

    def test_idempotency_key_stability(self):
        k1 = generate_idempotency_key("faigate", "faigate_local", "gateway_request", "101", "model_a")
        k2 = generate_idempotency_key("faigate", "faigate_local", "gateway_request", "101", "model_a")
        k3 = generate_idempotency_key("faigate", "faigate_local", "gateway_request", "102", "model_a")
        self.assertEqual(k1, k2)
        self.assertNotEqual(k1, k3)
        self.assertEqual(len(k1), 64)

    def test_synthetic_requests_collection_and_schema(self):
        synth_file = os.path.join(self.fixtures_dir, "synthetic_requests.json")
        with open(synth_file, "r") as f:
            rows = json.load(f)

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            con = sqlite3.connect(tmp_path)
            con.execute("""
            CREATE TABLE requests (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   REAL    NOT NULL,
                provider    TEXT    NOT NULL,
                model       TEXT    NOT NULL,
                layer       TEXT    NOT NULL,
                rule_name   TEXT    NOT NULL,
                prompt_tok  INTEGER DEFAULT 0,
                compl_tok   INTEGER DEFAULT 0,
                cache_hit   INTEGER DEFAULT 0,
                cache_miss  INTEGER DEFAULT 0,
                cost_usd    REAL    DEFAULT 0,
                latency_ms  REAL    DEFAULT 0,
                success     INTEGER DEFAULT 1,
                error       TEXT    DEFAULT '',
                requested_model TEXT DEFAULT 'auto',
                modality        TEXT DEFAULT 'chat',
                client_profile  TEXT DEFAULT 'generic',
                client_tag      TEXT DEFAULT '',
                decision_reason TEXT DEFAULT '',
                confidence      REAL DEFAULT 0,
                canonical_model TEXT DEFAULT '',
                lane_family     TEXT DEFAULT '',
                route_type      TEXT DEFAULT '',
                lane_cluster    TEXT DEFAULT '',
                selection_path  TEXT DEFAULT '',
                runtime_window_state TEXT DEFAULT '',
                recovered_recently INTEGER DEFAULT 0,
                last_recovered_issue_type TEXT DEFAULT '',
                decision_details TEXT DEFAULT '{}',
                attempt_order   TEXT DEFAULT '[]',
                route_summary TEXT DEFAULT '{}'
            )
            """)
            for r in rows:
                cols = ", ".join(r.keys())
                placeholders = ", ".join(["?"] * len(r))
                con.execute(f"INSERT INTO requests ({cols}) VALUES ({placeholders})", list(r.values()))
            con.commit()
            con.close()

            adapter = FaigateAdapter(FaigateConfig(db_path=tmp_path))
            res = adapter.collect()

            self.assertEqual(res.status, "success")
            self.assertEqual(res.exit_status, 0)
            self.assertEqual(len(res.records), 2)
            self.assertEqual(res.totals["total_requests"], 2)
            self.assertEqual(res.totals["total_prompt_tok"], 1500)
            self.assertEqual(res.totals["total_compl_tok"], 250)
            self.assertFalse(res.totals["billing_truth"])  # US-10 Invariant

            # Verify events
            self.assertEqual(len(res.events), 2)
            evt0 = res.events[0]
            self.assertEqual(evt0["session_id"], "ses_faigate123456")
            self.assertEqual(evt0["project_id"], "proj_ael987")
            self.assertFalse(evt0["billing_assertion"])

            # Verify observations
            self.assertEqual(len(res.observations), 2)
            self.assertEqual(res.observations[0]["unit"], "request")
            self.assertEqual(res.observations[0]["value"], 2)
            self.assertEqual(res.observations[1]["unit"], "token")
            self.assertEqual(res.observations[1]["value"], 1750)

            # Verify account run
            self.assertIsNotNone(res.account_run)
            self.assertEqual(res.account_run["status"], "success")
            self.assertEqual(res.account_run["coverage"]["confirmation"], "complete")

        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def test_real_sample_requests_collection(self):
        real_file = os.path.join(self.fixtures_dir, "real_sample_requests.json")
        with open(real_file, "r") as f:
            rows = json.load(f)

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            con = sqlite3.connect(tmp_path)
            con.execute("""
            CREATE TABLE requests (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   REAL    NOT NULL,
                provider    TEXT    NOT NULL,
                model       TEXT    NOT NULL,
                layer       TEXT    NOT NULL,
                rule_name   TEXT    NOT NULL,
                prompt_tok  INTEGER DEFAULT 0,
                compl_tok   INTEGER DEFAULT 0,
                cache_hit   INTEGER DEFAULT 0,
                cache_miss  INTEGER DEFAULT 0,
                cost_usd    REAL    DEFAULT 0,
                latency_ms  REAL    DEFAULT 0,
                success     INTEGER DEFAULT 1,
                error       TEXT    DEFAULT '',
                requested_model TEXT DEFAULT 'auto',
                modality        TEXT DEFAULT 'chat',
                client_profile  TEXT DEFAULT 'generic',
                client_tag      TEXT DEFAULT '',
                decision_reason TEXT DEFAULT '',
                confidence      REAL DEFAULT 0,
                canonical_model TEXT DEFAULT '',
                lane_family     TEXT DEFAULT '',
                route_type      TEXT DEFAULT '',
                lane_cluster    TEXT DEFAULT '',
                selection_path  TEXT DEFAULT '',
                runtime_window_state TEXT DEFAULT '',
                recovered_recently INTEGER DEFAULT 0,
                last_recovered_issue_type TEXT DEFAULT '',
                decision_details TEXT DEFAULT '{}',
                attempt_order   TEXT DEFAULT '[]',
                route_summary TEXT DEFAULT '{}'
            )
            """)
            for r in rows:
                cols = ", ".join(r.keys())
                placeholders = ", ".join(["?"] * len(r))
                con.execute(f"INSERT INTO requests ({cols}) VALUES ({placeholders})", list(r.values()))
            con.commit()
            con.close()

            adapter = FaigateAdapter(FaigateConfig(db_path=tmp_path))
            res = adapter.collect()
            self.assertEqual(res.status, "success")
            self.assertEqual(len(res.records), 5)
            self.assertEqual(res.totals["total_requests"], 5)
            self.assertTrue(res.totals["total_compl_tok"] > 0)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def test_missing_db_fails_gracefully(self):
        adapter = FaigateAdapter(FaigateConfig(db_path="/non/existent/path/faigate.db"))
        res = adapter.collect()
        self.assertEqual(res.status, "failure")
        self.assertEqual(res.exit_status, 1)
        self.assertEqual(len(res.errors), 1)
        self.assertEqual(res.errors[0]["classification"], "unverified")

    def test_zero_rows_produces_evidenced_zero(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            con = sqlite3.connect(tmp_path)
            con.execute("""
            CREATE TABLE requests (
                id INTEGER PRIMARY KEY,
                timestamp REAL, provider TEXT, model TEXT, layer TEXT, rule_name TEXT,
                prompt_tok INTEGER, compl_tok INTEGER, cache_hit INTEGER, cache_miss INTEGER,
                cost_usd REAL, latency_ms REAL, success INTEGER, error TEXT, requested_model TEXT,
                modality TEXT, client_profile TEXT, client_tag TEXT, decision_reason TEXT,
                confidence REAL, canonical_model TEXT, lane_family TEXT, route_type TEXT,
                lane_cluster TEXT, selection_path TEXT, runtime_window_state TEXT,
                recovered_recently INTEGER, last_recovered_issue_type TEXT,
                decision_details TEXT, attempt_order TEXT, route_summary TEXT
            )
            """)
            con.commit()
            con.close()

            adapter = FaigateAdapter(FaigateConfig(db_path=tmp_path))
            res = adapter.collect()
            self.assertEqual(res.status, "success")
            self.assertEqual(res.totals["total_requests"], 0)
            self.assertEqual(res.observations[0]["state"], "evidenced_zero")
            self.assertEqual(res.observations[0]["value"], 0)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)


class TestReconciliationRules(unittest.TestCase):
    """
    Direct tests for PRD US-15 invariants.
    """

    def test_us15_b_standard_reconciliation(self):
        # US-15.b: Provider 75,210, Gateway 74,930 => Delta -280, Coverage ~99.6277%
        res = calculate_reconciliation("requests", observed=74930, authoritative=75210)
        self.assertEqual(res.delta, -280)
        self.assertAlmostEqual(res.coverage_ratio, 74930 / 75210, places=6)
        self.assertEqual(res.status, "under_coverage")

    def test_us15_c_zero_values_no_division_by_zero(self):
        # US-15.c: Both 0 => kein Verbrauch
        res1 = calculate_reconciliation("requests", observed=0, authoritative=0)
        self.assertEqual(res1.status, "no_usage")
        self.assertIsNone(res1.coverage_ratio)

        # US-15.c: Observed > 0 while Provider 0 => Abweichung, Quotient undefiniert
        res2 = calculate_reconciliation("requests", observed=50, authoritative=0)
        self.assertEqual(res2.status, "deviation_undefined")
        self.assertIsNone(res2.coverage_ratio)

    def test_us15_d_over_coverage_unclipped(self):
        # US-15.d: Coverage > 100% shown unclipped and flagged as over_coverage
        res = calculate_reconciliation("requests", observed=120, authoritative=100)
        self.assertEqual(res.status, "over_coverage")
        self.assertEqual(res.coverage_ratio, 1.2)
        self.assertIn("120.0000%", res.note)

    def test_us15_a_incomparable_scope(self):
        # US-15.a: Incompatible scope => not_comparable
        res = calculate_reconciliation("requests", observed=100, authoritative=100, is_comparable=False, incomparable_reason="Mismatched time intervals")
        self.assertEqual(res.status, "not_comparable")
        self.assertIsNone(res.coverage_ratio)
        self.assertEqual(res.note, "Mismatched time intervals")


if __name__ == "__main__":
    unittest.main()
