"""
Contract and Schema Validation Test Suite for Faigate Collector.
Enforces that FaigateAdapter outputs strictly satisfy schema/collector constraints
via schema/collector/tests/validate.py.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest

# Ensure main repo root is discoverable for schema/collector validation import
repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../../.."))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from collectors.faigate.adapter import FaigateAdapter, FaigateConfig
from schema.collector.tests.validate import schema_errors, semantic_errors


class TestFaigateContractValidation(unittest.TestCase):

    def setUp(self):
        self.fixtures_dir = os.path.join(os.path.dirname(__file__), "fixtures")

    def test_schema_and_semantics_on_synthetic_run(self):
        synth_file = os.path.join(self.fixtures_dir, "synthetic_requests.json")
        with open(synth_file) as f:
            rows = json.load(f)

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            con = sqlite3.connect(tmp_path)
            con.execute("""CREATE TABLE requests (
                id INTEGER PRIMARY KEY, timestamp REAL, provider TEXT, model TEXT, layer TEXT, rule_name TEXT,
                prompt_tok INTEGER, compl_tok INTEGER, cache_hit INTEGER, cache_miss INTEGER,
                cost_usd REAL, latency_ms REAL, success INTEGER, error TEXT, requested_model TEXT,
                modality TEXT, client_profile TEXT, client_tag TEXT, decision_reason TEXT,
                confidence REAL, canonical_model TEXT, lane_family TEXT, route_type TEXT,
                lane_cluster TEXT, selection_path TEXT, runtime_window_state TEXT,
                recovered_recently INTEGER, last_recovered_issue_type TEXT,
                decision_details TEXT, attempt_order TEXT, route_summary TEXT
            )""")
            for r in rows:
                cols = ", ".join(r.keys())
                q = ", ".join(["?"] * len(r))
                con.execute(f"INSERT INTO requests ({cols}) VALUES ({q})", list(r.values()))
            con.commit()
            con.close()

            adapter = FaigateAdapter(FaigateConfig(db_path=tmp_path))
            res = adapter.collect()

            # Run record validation
            s_err = schema_errors(res.run_record, "run")
            sem_err = semantic_errors("run", res.run_record)
            self.assertEqual(s_err, [], f"Run schema errors: {s_err}")
            self.assertEqual(sem_err, [], f"Run semantic errors: {sem_err}")

            # Capability validation
            for cap in res.capabilities:
                s_err = schema_errors(cap, "capability")
                sem_err = semantic_errors("capability", cap)
                self.assertEqual(s_err, [], f"Capability schema errors: {s_err}")
                self.assertEqual(sem_err, [], f"Capability semantic errors: {sem_err}")

            # Observation validation
            for obs in res.observations:
                s_err = schema_errors(obs, "observation")
                sem_err = semantic_errors("observation", obs)
                self.assertEqual(s_err, [], f"Observation schema errors: {s_err}")
                self.assertEqual(sem_err, [], f"Observation semantic errors: {sem_err}")

        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def test_schema_and_semantics_on_real_samples(self):
        real_file = os.path.join(self.fixtures_dir, "real_sample_requests.json")
        with open(real_file) as f:
            rows = json.load(f)

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            con = sqlite3.connect(tmp_path)
            con.execute("""CREATE TABLE requests (
                id INTEGER PRIMARY KEY, timestamp REAL, provider TEXT, model TEXT, layer TEXT, rule_name TEXT,
                prompt_tok INTEGER, compl_tok INTEGER, cache_hit INTEGER, cache_miss INTEGER,
                cost_usd REAL, latency_ms REAL, success INTEGER, error TEXT, requested_model TEXT,
                modality TEXT, client_profile TEXT, client_tag TEXT, decision_reason TEXT,
                confidence REAL, canonical_model TEXT, lane_family TEXT, route_type TEXT,
                lane_cluster TEXT, selection_path TEXT, runtime_window_state TEXT,
                recovered_recently INTEGER, last_recovered_issue_type TEXT,
                decision_details TEXT, attempt_order TEXT, route_summary TEXT
            )""")
            for r in rows:
                cols = ", ".join(r.keys())
                q = ", ".join(["?"] * len(r))
                con.execute(f"INSERT INTO requests ({cols}) VALUES ({q})", list(r.values()))
            con.commit()
            con.close()

            adapter = FaigateAdapter(FaigateConfig(db_path=tmp_path))
            res = adapter.collect()

            s_err = schema_errors(res.run_record, "run")
            sem_err = semantic_errors("run", res.run_record)
            self.assertEqual(s_err, [])
            self.assertEqual(sem_err, [])

            for cap in res.capabilities:
                self.assertEqual(schema_errors(cap, "capability"), [])
                self.assertEqual(semantic_errors("capability", cap), [])

            for obs in res.observations:
                self.assertEqual(schema_errors(obs, "observation"), [])
                self.assertEqual(semantic_errors("observation", obs), [])

        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)


if __name__ == "__main__":
    unittest.main()
