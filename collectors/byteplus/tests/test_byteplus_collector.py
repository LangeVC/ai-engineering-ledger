#!/usr/bin/env python3
"""Comprehensive Acceptance Criteria & Integration Tests for PRD US-08.

Tests strictly cover:
- US-08.a: Für jedes der fünf Konten werden nur nach Capability-Prüfung verfügbare Quota-,
  Usage- und Billingdaten erfasst; Kontoalias und Produktdomäne sind in jedem Datensatz enthalten.
- US-08.b: Eine Fixture mit Coding-Plan-Quota und separater Inference-Usage erzeugt zwei getrennte
  Reihen; die Quotaansicht enthält keine aus Inference abgeleiteten Coding-Plan-Tokens.
- US-08.c: Schlägt bp03 fehl, werden vier erfolgreiche Accountläufe gespeichert und der Gesamtlauf
  als teilweise fehlgeschlagen gemeldet (status='partial_failure', exit_status=3); kein Account
  übernimmt Daten eines anderen.
"""

import copy
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

from collectors.byteplus.collector import (
    BytePlusCollector,
    BytePlusObservation,
    AccountCollectionResult,
    BytePlusRunResult,
    DOMAIN_CODING_PLAN,
    DOMAIN_INFERENCE,
    DOMAIN_BILLING,
    DEFAULT_PROFILES,
)
from collectors.account import (
    BytePlusProfileCoordinator,
    CAPABILITY_READY,
    CAPABILITY_UNSUPPORTED,
    CAPABILITY_UNVERIFIED,
)


class TestBytePlusMultiAccountUS08(unittest.TestCase):

    def setUp(self):
        self.collector = BytePlusCollector()

    def test_us08_a_five_accounts_distinct_with_capability_gating_and_domain_in_every_record(self):
        """
        US-08.a: Für jedes der fünf Konten werden nur nach Capability-Prüfung verfügbare Quota-,
        Usage- und Billingdaten erfasst; Kontoalias und Produktdomäne sind in jedem Datensatz enthalten.
        """
        expected_profiles = ["bp01", "bp02", "bp03", "bp04", "bp05"]
        self.assertEqual(self.collector.profiles, expected_profiles)

        # Run multi-account collection for all 5 accounts with distinct synthetic data
        sim_data = {
            p: {
                "quota_data": {
                    "quota_limit": 500000 + i * 10000,
                    "quota_used": 100000 + i * 5000,
                    "quota_remaining": 400000 + i * 5000,
                    "quota_percent_used": 20 + i,
                },
                "inference_data": {
                    "model": "deepseek-v3",
                    "input_tokens": 50000 + i * 1000,
                    "output_tokens": 12000 + i * 500,
                    "requests": 25 + i,
                },
                "billing_data": {
                    "cost": "12.50",
                    "currency": "USD",
                }
            }
            for i, p in enumerate(expected_profiles)
        }

        run_result = self.collector.collect_all_accounts(
            trigger="scheduler",
            period_start="2026-10-26T00:00:00Z",
            period_end="2026-10-26T01:00:00Z",
            simulated_accounts_input=sim_data,
        )

        self.assertEqual(run_result.status, "success")
        self.assertEqual(run_result.exit_status, 0)

        # Verify all 5 profiles are present and accounted for
        account_runs = run_result.account_runs
        seen_profiles = set()
        for ar in account_runs:
            seen_profiles.add(ar.account_alias)
            self.assertEqual(ar.account_alias, ar.profile_id)
            self.assertIn(ar.account_alias, expected_profiles)

            # Account run record must contain source, account_alias, and product_domain
            ar_dict = ar.to_account_run_dict()
            self.assertEqual(ar_dict["source"], "byteplus")
            self.assertEqual(ar_dict["account_alias"], ar.account_alias)
            self.assertIn(ar_dict["product_domain"], [DOMAIN_CODING_PLAN, DOMAIN_INFERENCE, DOMAIN_BILLING])

            # Every observation record must contain account_alias and product_domain
            for obs in ar.observations:
                obs_dict = obs.to_observation_dict()
                self.assertEqual(obs_dict["account_alias"], ar.account_alias, "Observation must contain account_alias")
                self.assertEqual(obs_dict["product_domain"], ar.product_domain, "Observation must contain product_domain")
                self.assertIn(obs_dict["state"], ["value", "evidenced_zero", "partial"])

        self.assertEqual(seen_profiles, set(expected_profiles), "All 5 profiles must be executed")

    def test_us08_b_strict_separation_of_coding_plan_quota_and_inference_usage(self):
        """
        US-08.b: Eine Fixture mit Coding-Plan-Quota und separater Inference-Usage erzeugt zwei getrennte
        Reihen; die Quotaansicht enthält keine aus Inference abgeleiteten Coding-Plan-Tokens.
        """
        account = "bp01"
        fixture_quota = {
            "quota_limit": 500000,
            "quota_used": 150000,
            "quota_remaining": 350000,
            "quota_percent_used": 30,
            "quota_unit": "request",
        }
        fixture_inference = {
            "model": "deepseek-r1",
            "input_tokens": 82000,
            "output_tokens": 15000,
            "cache_hit_tokens": 60000,
            "cache_miss_tokens": 22000,
            "reasoning_tokens": 9000,
            "requests": 14,
        }

        # 1. Parse both domains separately
        obs_quota = self.collector.parse_coding_plan_quota(account, fixture_quota)
        obs_inference = self.collector.parse_inference_usage(account, fixture_inference)

        # Verify distinct row generation
        self.assertGreater(len(obs_quota), 0)
        self.assertGreater(len(obs_inference), 0)

        # Verify domains
        for o in obs_quota:
            self.assertEqual(o.product_domain, DOMAIN_CODING_PLAN)
            self.assertNotEqual(o.product_domain, DOMAIN_INFERENCE)
            # US-08.b invariant: Quota view contains NO inference-derived token metrics
            self.assertNotIn("token", o.unit.lower(), "Coding plan quota should not use token units from inference")
            self.assertNotIn("inference", o.metric.lower())

        for o in obs_inference:
            self.assertEqual(o.product_domain, DOMAIN_INFERENCE)
            self.assertNotEqual(o.product_domain, DOMAIN_CODING_PLAN)

        # 2. Transform into canonical events and measurements
        all_obs = obs_quota + obs_inference
        ev_id, _ = self.collector.build_evidence_descriptor(b"fixture_payload")
        events, measurements = self.collector.observations_to_events_and_measurements(
            all_obs, raw_evidence_id=ev_id
        )

        # There must be two distinct event streams/groups: one for coding plan, one for inference
        self.assertEqual(len(events), 2)
        event_domains = {ev["payload"]["product_domain"] for ev in events}
        self.assertEqual(event_domains, {DOMAIN_CODING_PLAN, DOMAIN_INFERENCE})

        # Measurement verification
        quota_measurements = [m for m in measurements if "quota" in m["metric"]]
        inference_measurements = [m for m in measurements if "token" in m["metric"]]

        self.assertGreater(len(quota_measurements), 0)
        self.assertGreater(len(inference_measurements), 0)

        # Ensure no cross-contamination
        for qm in quota_measurements:
            self.assertNotIn(qm["metric"], ["input_tokens", "output_tokens", "cache_hit_tokens", "reasoning_tokens"])
            self.assertEqual(qm["unit"], "request" if qm["metric"] != "quota_percent_used" else "percent")

    def test_us08_c_bp03_failure_isolation_and_partial_failure_exit(self):
        """
        US-08.c: Schlägt bp03 fehl, werden vier erfolgreiche Accountläufe gespeichert und der
        Gesamtlauf als teilweise fehlgeschlagen gemeldet (status='partial_failure', exit_status=3);
        kein Account übernimmt Daten eines anderen.
        """
        profiles = ["bp01", "bp02", "bp03", "bp04", "bp05"]
        sim_data = {
            "bp01": {"quota_data": {"quota_limit": 500000, "quota_used": 10000, "quota_remaining": 490000}},
            "bp02": {"quota_data": {"quota_limit": 500000, "quota_used": 20000, "quota_remaining": 480000}},
            "bp03": {
                "simulate_failure": True,
                "error_class": "auth",
                "http_status": 401,
                "error_message": "Invalid authentication token Bearer sk-bp03-secret-key-123456",
            },
            "bp04": {"quota_data": {"quota_limit": 500000, "quota_used": 40000, "quota_remaining": 460000}},
            "bp05": {"quota_data": {"quota_limit": 500000, "quota_used": 50000, "quota_remaining": 450000}},
        }

        run_result = self.collector.collect_all_accounts(
            trigger="scheduler",
            period_start="2026-10-26T00:00:00Z",
            period_end="2026-10-26T01:00:00Z",
            simulated_accounts_input=sim_data,
        )

        run_dict = run_result.to_run_dict()

        # 1. Assert overall run status is partial_failure and exit_status is 3 (ADR-0002)
        self.assertEqual(run_dict["status"], "partial_failure", "Run status must be partial_failure when bp03 fails")
        self.assertEqual(run_dict["exit_status"], 3, "Exit status must be 3 for partial_failure")

        # 2. Check individual account runs
        account_runs = run_dict["account_runs"]
        success_runs = [ar for ar in account_runs if ar["status"] == "success"]
        failure_runs = [ar for ar in account_runs if ar["status"] == "failure"]

        self.assertEqual(len(success_runs), 4, "Exactly four accounts must succeed")
        self.assertEqual(len(failure_runs), 1, "Exactly one account must fail")

        # Verify bp03 is the failed account
        self.assertEqual(failure_runs[0]["account_alias"], "bp03")
        self.assertGreaterEqual(len(failure_runs[0]["errors"]), 1)
        err = failure_runs[0]["errors"][0]
        self.assertEqual(err["error_class"], "auth")
        self.assertEqual(err["http_status"], 401)
        # Verify secret was redacted and not leaked
        self.assertNotIn("sk-bp03-secret-key-123456", err["message"])

        # 3. Verify no cross-account data inheritance
        success_aliases = {ar["account_alias"] for ar in success_runs}
        self.assertEqual(success_aliases, {"bp01", "bp02", "bp04", "bp05"})

        # Check raw payloads of successful accounts
        bp01_run = [ar for ar in run_result.account_runs if ar.account_alias == "bp01"][0]
        bp02_run = [ar for ar in run_result.account_runs if ar.account_alias == "bp02"][0]
        bp04_run = [ar for ar in run_result.account_runs if ar.account_alias == "bp04"][0]
        bp05_run = [ar for ar in run_result.account_runs if ar.account_alias == "bp05"][0]

        self.assertEqual(bp01_run.raw_payloads[0]["quota_used"], 10000)
        self.assertEqual(bp02_run.raw_payloads[0]["quota_used"], 20000)
        self.assertEqual(bp04_run.raw_payloads[0]["quota_used"], 40000)
        self.assertEqual(bp05_run.raw_payloads[0]["quota_used"], 50000)

    def test_routing_subset_filtering(self):
        """Tests that routing subset allows selective execution of accounts."""
        run_res = self.collector.collect_all_accounts(
            routing_subset=["bp01", "bp02"],
        )
        self.assertEqual(run_res.status, "success")
        self.assertEqual(len(run_res.account_runs), 2)
        aliases = [ar.account_alias for ar in run_res.account_runs]
        self.assertEqual(aliases, ["bp01", "bp02"])


if __name__ == "__main__":
    unittest.main()
