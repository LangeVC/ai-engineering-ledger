#!/usr/bin/env python3
"""Contract validation test using schema/collector/tests/validate.py and
schema/core/tests/minival.py to verify generated structures strictly against AEL v0.1 schemas.
"""

import copy
import json
import os
import sys
import unittest
from decimal import Decimal

# Locate main repository root containing schema/ and worktree collectors root
TEST_DIR = os.path.dirname(os.path.abspath(__file__))
# Finde Repo-Root dynamisch anhand von schema/
curr = TEST_DIR
REPO_ROOT = None
while curr and curr != "/":
    if os.path.isdir(os.path.join(curr, "schema", "core")):
        REPO_ROOT = curr
        break
    curr = os.path.dirname(curr)
if not REPO_ROOT:
    REPO_ROOT = os.path.abspath(os.path.join(TEST_DIR, "../../../../../.."))
WORKTREE_ROOT = REPO_ROOT

sys.path.insert(0, WORKTREE_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "schema", "core"))
sys.path.insert(0, os.path.join(REPO_ROOT, "schema", "core", "tests"))
sys.path.insert(0, os.path.join(REPO_ROOT, "schema", "collector", "tests"))

from collectors.deepseek.collector import DeepSeekCollector, DeepSeekConfig
import minival, semantic
import validate as collector_validate

L_CORE = minival.Loader(os.path.join(REPO_ROOT, "schema", "core"))
CAT_CORE = L_CORE.doc("units.catalog.json")

SAMPLE_CSV = """date,model,requests,prompt_tokens,prompt_cache_hit_tokens,prompt_cache_miss_tokens,completion_tokens,reasoning_tokens,cost
2026-10-25,deepseek-chat,14,28450,22100,6350,4120,0,0.007621
2026-10-25,deepseek-reasoner,3,15800,8500,7300,3450,2100,0.015240
"""


class TestDeepSeekContracts(unittest.TestCase):

    def setUp(self):
        self.collector = DeepSeekCollector()

    def test_schema_collector_capability_contract(self):
        cap = self.collector.declare_capability(checked_at="2026-10-26T12:00:00Z")
        errs = collector_validate.schema_errors(cap, "capability")
        self.assertEqual(errs, [], f"Capability schema errors: {errs}")

    def test_schema_collector_account_contract(self):
        acc = self.collector.declare_account(checked_at="2026-10-26T12:00:00Z")
        errs = collector_validate.schema_errors(acc, "account")
        self.assertEqual(errs, [], f"Account schema errors: {errs}")
        sem_errs = collector_validate.semantic_errors("account", acc)
        self.assertEqual(sem_errs, [], f"Account semantic errors: {sem_errs}")

    def test_schema_collector_run_contract_success(self):
        records, report = self.collector.parse_export_data(SAMPLE_CSV, format_type="csv")
        run_obj = self.collector.build_collection_run(
            report=report,
            started_at="2026-10-26T12:00:00Z",
            finished_at="2026-10-26T12:00:01Z",
            period_start="2026-10-25T00:00:00Z",
            period_end="2026-10-25T23:59:59Z",
        )
        errs = collector_validate.schema_errors(run_obj, "run")
        self.assertEqual(errs, [], f"Run schema errors: {errs}")
        sem_errs = collector_validate.semantic_errors("run", run_obj)
        self.assertEqual(sem_errs, [], f"Run semantic errors: {sem_errs}")

    def test_schema_core_evidence_contract(self):
        raw_bytes = SAMPLE_CSV.encode("utf-8")
        ev_id, desc = self.collector.build_evidence_descriptor(
            raw_bytes,
            created_at="2026-10-26T12:00:00Z",
        )
        errs = minival.validate(desc, "evidence.schema.json", L_CORE)
        self.assertEqual(errs, [], f"Evidence schema errors: {errs}")
        art_errs = semantic.verify_artifact(raw_bytes, desc)
        self.assertEqual(art_errs, [], f"Artifact verification errors: {art_errs}")

    def test_schema_core_event_and_measurements_contract(self):
        raw_bytes = SAMPLE_CSV.encode("utf-8")
        ev_id, desc = self.collector.build_evidence_descriptor(
            raw_bytes,
            created_at="2026-10-26T12:00:00Z",
        )
        records, report = self.collector.parse_export_data(SAMPLE_CSV, format_type="csv")
        events, measurements = self.collector.records_to_events_and_measurements(
            records=records,
            raw_evidence_id=ev_id,
            collected_at="2026-10-26T12:00:00Z",
        )
        self.assertEqual(len(events), 2)
        for ev in events:
            errs = minival.validate(ev, "event.schema.json", L_CORE)
            self.assertEqual(errs, [], f"Event schema errors: {errs}")
            sem_errs = semantic.check_event(ev)
            self.assertEqual(sem_errs, [], f"Event semantic errors: {sem_errs}")

        for m in measurements:
            errs = minival.validate(m, "measurement.schema.json", L_CORE)
            self.assertEqual(errs, [], f"Measurement schema errors: {errs}")
            sem_errs = semantic.check_measurement(m, CAT_CORE)
            self.assertEqual(sem_errs, [], f"Measurement semantic errors: {sem_errs}")

        # Check evidence bindings
        binding_errs = semantic.check_bindings(measurements, [desc])
        self.assertEqual(binding_errs, [], f"Evidence binding errors: {binding_errs}")


if __name__ == "__main__":
    unittest.main()
