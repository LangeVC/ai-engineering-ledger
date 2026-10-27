#!/usr/bin/env python3
"""Contract validation tests for BytePlus Collector using minival and collector validate.py."""

import json
import os
import sys
import unittest
from decimal import Decimal

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
# Locate repo root
curr = TEST_DIR
REPO_ROOT = None
while curr and curr != "/":
    if os.path.isdir(os.path.join(curr, "schema", "core")):
        REPO_ROOT = curr
        break
    curr = os.path.dirname(curr)
if not REPO_ROOT:
    REPO_ROOT = os.path.abspath(os.path.join(TEST_DIR, "../../.."))

WORKTREE_ROOT = os.path.abspath(os.path.join(TEST_DIR, "../../.."))

sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, WORKTREE_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "schema", "core"))
sys.path.insert(0, os.path.join(REPO_ROOT, "schema", "core", "tests"))
sys.path.insert(0, os.path.join(REPO_ROOT, "schema", "collector", "tests"))

from collectors.byteplus.collector import (
    BytePlusCollector,
    DOMAIN_CODING_PLAN,
    DOMAIN_INFERENCE,
    DOMAIN_BILLING,
)
import minival, semantic
import validate as collector_validate

L_CORE = minival.Loader(os.path.join(REPO_ROOT, "schema", "core"))
CAT_CORE = L_CORE.doc("units.catalog.json")


class TestBytePlusContracts(unittest.TestCase):

    def setUp(self):
        self.collector = BytePlusCollector()

    def test_schema_collector_capability_contract(self):
        cap = self.collector.declare_capability(
            account_alias="bp01",
            product_domain=DOMAIN_CODING_PLAN,
            capability="quota",
            status="ready",
            checked_at="2026-10-26T12:00:00Z",
        )
        errs = collector_validate.schema_errors(cap, "capability")
        self.assertEqual(errs, [], f"Capability schema errors: {errs}")

    def test_schema_collector_account_contract(self):
        acc = self.collector.declare_account(
            account_alias="bp01",
            checked_at="2026-10-26T12:00:00Z",
        )
        errs = collector_validate.schema_errors(acc, "account")
        self.assertEqual(errs, [], f"Account schema errors: {errs}")
        sem_errs = collector_validate.semantic_errors("account", acc)
        self.assertEqual(sem_errs, [], f"Account semantic errors: {sem_errs}")

    def test_schema_collector_observation_contract(self):
        obs_list = self.collector.parse_coding_plan_quota(
            account_alias="bp01",
            data={"quota_limit": 500000, "quota_used": 100000, "quota_remaining": 400000},
            observed_at="2026-10-26T12:00:00Z",
        )
        self.assertGreaterEqual(len(obs_list), 3)
        for obs in obs_list:
            obs_dict = obs.to_observation_dict()
            errs = collector_validate.schema_errors(obs_dict, "observation")
            self.assertEqual(errs, [], f"Observation schema errors: {errs}")

    def test_schema_collector_run_contract_success(self):
        run_res = self.collector.collect_all_accounts(
            trigger="scheduler",
            period_start="2026-10-26T00:00:00Z",
            period_end="2026-10-26T01:00:00Z",
        )
        run_dict = run_res.to_run_dict()
        errs = collector_validate.schema_errors(run_dict, "run")
        self.assertEqual(errs, [], f"Run schema errors: {errs}")
        sem_errs = collector_validate.semantic_errors("run", run_dict)
        self.assertEqual(sem_errs, [], f"Run semantic errors: {sem_errs}")

    def test_schema_collector_run_contract_partial_failure(self):
        # bp03 fails, others succeed
        sim_input = {
            "bp03": {"simulate_failure": True, "error_message": "Invalid API key for bp03", "http_status": 401}
        }
        run_res = self.collector.collect_all_accounts(
            trigger="scheduler",
            period_start="2026-10-26T00:00:00Z",
            period_end="2026-10-26T01:00:00Z",
            simulated_accounts_input=sim_input,
        )
        run_dict = run_res.to_run_dict()
        self.assertEqual(run_dict["status"], "partial_failure")
        self.assertEqual(run_dict["exit_status"], 3)
        errs = collector_validate.schema_errors(run_dict, "run")
        self.assertEqual(errs, [], f"Partial failure Run schema errors: {errs}")
        sem_errs = collector_validate.semantic_errors("run", run_dict)
        self.assertEqual(sem_errs, [], f"Partial failure Run semantic errors: {sem_errs}")

    def test_schema_core_events_and_measurements_contract(self):
        raw_bytes = b'{"quota_limit": 500000, "quota_used": 100000, "quota_remaining": 400000}'
        ev_id, desc = self.collector.build_evidence_descriptor(raw_bytes, created_at="2026-10-26T12:00:00Z")
        errs = minival.validate(desc, "evidence.schema.json", L_CORE)
        self.assertEqual(errs, [], f"Evidence schema errors: {errs}")

        obs_list = self.collector.parse_coding_plan_quota(
            account_alias="bp01",
            data={"quota_limit": 500000, "quota_used": 100000, "quota_remaining": 400000},
            observed_at="2026-10-26T12:00:00Z",
            evidence_ref=ev_id,
        )
        events, measurements = self.collector.observations_to_events_and_measurements(
            observations=obs_list,
            raw_evidence_id=ev_id,
            collected_at="2026-10-26T12:00:00Z",
        )
        self.assertEqual(len(events), 1)
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

        binding_errs = semantic.check_bindings(measurements, [desc])
        self.assertEqual(binding_errs, [], f"Evidence binding errors: {binding_errs}")


if __name__ == "__main__":
    unittest.main()
