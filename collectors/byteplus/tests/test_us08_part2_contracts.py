#!/usr/bin/env python3
"""Contract and behavioral tests for US-08.d and Part 2 requirements:
- US-08.d: Partielle oder paginierte Billingantworten werden erst nach vollständiger Prüfung als vollständig markiert.
- US-08.d: Nachgelieferte Abrechnung ersetzt die wirksame Revision ohne zweite Belastung.
- Erneuerungszyklen und Schwellenwert-Beobachtung (TS §12).
- Historische Snapshot-Vergleiche für bp01..bp05.
- Strikte Schema-Konformität (collector/run.schema.json, core/revision.schema.json, core/measurement.schema.json).
"""

import copy
import json
import os
import sys
import unittest
from decimal import Decimal

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
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

import minival, semantic
import validate as collector_validate

from collectors.byteplus.collector import (
    BytePlusCollector,
    BytePlusObservation,
    AccountCollectionResult,
    DOMAIN_CODING_PLAN,
    DOMAIN_INFERENCE,
    DOMAIN_BILLING,
    generate_ulid,
)

L_CORE = minival.Loader(os.path.join(REPO_ROOT, "schema", "core"))
CAT_CORE = L_CORE.doc("units.catalog.json")


class TestBytePlusPart2Contracts(unittest.TestCase):
    def setUp(self):
        self.collector = BytePlusCollector()

    def test_us08_d_paginated_billing_partial_until_fully_verified(self):
        """
        US-08.d: Partielle oder paginierte Billingantworten werden erst nach vollständiger Prüfung als vollständig markiert.
        Tests 3 scenarios:
        1. Incomplete / truncated pages -> confirmation remains 'partial' (not complete).
        2. Unverified pages (verify_complete_fn fails) -> confirmation remains 'partial'.
        3. Complete and verified pages -> confirmation becomes 'complete', pagination complete=True.
        """
        # Scenario 1: Multi-page response truncated after page 1
        page1 = {
            "cursor": "cursor_pg1",
            "next_cursor": "cursor_pg2",
            "has_more": True,
            "truncated": True,
            "records": [{"invoice_id": "inv_101", "cost": "25.50", "currency": "USD"}],
        }
        res_truncated = self.collector.collect_paginated_billing(
            account_alias="bp01",
            pages=[page1],
            period_start="2026-10-01T00:00:00Z",
            period_end="2026-10-26T00:00:00Z",
        )
        dict_trunc = res_truncated.to_account_run_dict()
        self.assertEqual(dict_trunc["coverage"]["confirmation"], "partial")
        self.assertFalse(dict_trunc["coverage"]["pagination"]["complete"])
        self.assertTrue(dict_trunc["coverage"]["pagination"]["truncated"])
        self.assertIsNone(dict_trunc["coverage"]["confirmed"])
        # Check observations state is partial
        for obs in res_truncated.observations:
            self.assertEqual(obs.state, "partial")
        # Validate schema
        # Wrap account run in full run to validate against run.schema.json
        full_run_trunc = {
            "schema_version": "0.1",
            "run_id": "run_test_trunc",
            "trigger": "scheduler",
            "started_at": "2026-10-26T00:00:00Z",
            "finished_at": "2026-10-26T01:00:00Z",
            "status": "success",
            "exit_status": 0,
            "routing_subset": [],
            "account_runs": [dict_trunc],
        }
        errs = collector_validate.schema_errors(full_run_trunc, "run")
        self.assertEqual(errs, [], f"Schema errors in truncated account run: {errs}")

        # Scenario 2: All pages present but verification check fails (e.g. hash mismatch / sum mismatch)
        page1_clean = {
            "cursor": "cursor_pg1",
            "next_cursor": "cursor_pg2",
            "has_more": True,
            "truncated": False,
            "records": [{"invoice_id": "inv_101", "cost": "25.50", "currency": "USD"}],
        }
        page2_clean = {
            "cursor": "cursor_pg2",
            "next_cursor": None,
            "has_more": False,
            "truncated": False,
            "records": [{"invoice_id": "inv_102", "cost": "15.00", "currency": "USD"}],
        }
        # Verification requires expected 3 records, but only 2 were delivered
        def verify_fails(records):
            return len(records) == 3

        res_unverified = self.collector.collect_paginated_billing(
            account_alias="bp01",
            pages=[page1_clean, page2_clean],
            period_start="2026-10-01T00:00:00Z",
            period_end="2026-10-26T00:00:00Z",
            verify_complete_fn=verify_fails,
        )
        dict_unverified = res_unverified.to_account_run_dict()
        self.assertEqual(dict_unverified["coverage"]["confirmation"], "partial")
        self.assertFalse(dict_unverified["coverage"]["pagination"]["complete"])
        self.assertIsNone(dict_unverified["coverage"]["confirmed"])

        # Scenario 3: Complete and verified
        def verify_succeeds(records):
            return len(records) == 2 and sum(Decimal(r["cost"]) for r in records) == Decimal("40.50")

        res_complete = self.collector.collect_paginated_billing(
            account_alias="bp01",
            pages=[page1_clean, page2_clean],
            period_start="2026-10-01T00:00:00Z",
            period_end="2026-10-26T00:00:00Z",
            verify_complete_fn=verify_succeeds,
        )
        dict_complete = res_complete.to_account_run_dict()
        self.assertEqual(dict_complete["coverage"]["confirmation"], "complete")
        self.assertTrue(dict_complete["coverage"]["pagination"]["complete"])
        self.assertFalse(dict_complete["coverage"]["pagination"]["truncated"])
        self.assertIsNotNone(dict_complete["coverage"]["confirmed"])
        self.assertIsNone(dict_complete["coverage"]["pagination"]["next_cursor"])
        for obs in res_complete.observations:
            self.assertEqual(obs.state, "value")

        full_run_complete = {
            "schema_version": "0.1",
            "run_id": "run_test_complete",
            "trigger": "scheduler",
            "started_at": "2026-10-26T00:00:00Z",
            "finished_at": "2026-10-26T01:00:00Z",
            "status": "success",
            "exit_status": 0,
            "routing_subset": [],
            "account_runs": [dict_complete],
        }
        errs = collector_validate.schema_errors(full_run_complete, "run")
        self.assertEqual(errs, [], f"Schema errors in complete account run: {errs}")

    def test_us08_d_late_arriving_billing_replaces_effective_revision_without_double_charge(self):
        """
        US-08.d: Nachgelieferte Abrechnung ersetzt die wirksame Revision ohne zweite Belastung.
        Verifies:
        - Prior revision is marked effective=False.
        - Superseding revision has revision_no = prior.revision_no + 1, supersedes = prior.revision_id.
        - Only ONE revision remains effective.
        - Semantic check semantic.check_revision_chain passes without cycles or dangling links.
        - Prior value is not double counted.
        """
        ev_id_1, desc1 = self.collector.build_evidence_descriptor(b'{"initial": 1}', account_alias="bp02")
        ev_id_2, desc2 = self.collector.build_evidence_descriptor(b'{"finalized": 2}', account_alias="bp02")

        # Initial billing capture (provisional invoice)
        initial_records = [{"invoice_id": "inv_bp02_202610", "cost": "30.00", "currency": "usd"}]
        initial_meas, initial_chain = self.collector.reconcile_superseding_billing(
            account_alias="bp02",
            prior_measurements=[],
            new_billing_records=initial_records,
            raw_evidence_id=ev_id_1,
            recorded_at="2026-10-20T00:00:00Z",
        )
        self.assertEqual(len(initial_meas), 1)
        self.assertEqual(len(initial_chain), 1)
        self.assertTrue(initial_chain[0]["effective"])
        self.assertIsNone(initial_chain[0]["supersedes"])
        self.assertEqual(initial_chain[0]["revision_no"], 1)

        # Validate initial revision schema & semantics
        errs = minival.validate(initial_chain[0], "revision.schema.json", L_CORE)
        self.assertEqual(errs, [], f"Initial revision schema error: {errs}")
        chain_errs = semantic.check_revision_chain(initial_chain)
        self.assertEqual(chain_errs, [], f"Initial revision chain error: {chain_errs}")

        # Late arriving settlement (authoritative invoice finalized at 35.00 USD)
        late_records = [{"invoice_id": "inv_bp02_202610", "cost": "35.00", "currency": "usd"}]
        updated_meas, updated_chain = self.collector.reconcile_superseding_billing(
            account_alias="bp02",
            prior_measurements=initial_meas,
            new_billing_records=late_records,
            raw_evidence_id=ev_id_2,
            reason="Late settlement invoice finalized by BytePlus",
            recorded_at="2026-10-26T12:00:00Z",
        )

        self.assertEqual(len(updated_meas), 1)
        self.assertEqual(len(updated_chain), 2)

        # Check revision chain: prior must be False, new must be True
        rev1 = updated_chain[0]
        rev2 = updated_chain[1]
        self.assertFalse(rev1["effective"], "Prior revision must no longer be effective (no double charge)")
        self.assertTrue(rev2["effective"], "New superseding revision must be effective")
        self.assertEqual(rev2["supersedes"], rev1["revision_id"])
        self.assertEqual(rev2["revision_no"], 2)
        self.assertEqual(rev2["reason_code"], "provider_correction")

        # Validate semantic revision chain rule: exactly one effective revision
        chain_errs = semantic.check_revision_chain(updated_chain)
        self.assertEqual(chain_errs, [], f"Updated revision chain error: {chain_errs}")

        # Validate measurement schema & semantic checks
        m_new = updated_meas[0]
        errs = minival.validate(m_new, "measurement.schema.json", L_CORE)
        self.assertEqual(errs, [], f"Updated measurement schema error: {errs}")
        sem_errs = semantic.check_measurement(m_new, CAT_CORE)
        self.assertEqual(sem_errs, [], f"Updated measurement semantic error: {sem_errs}")

        # Total effective cost must be 35.00, NOT 30 + 35 = 65.00
        effective_measurements = [m for m in [initial_meas[0], m_new] if m["revision"]["effective"]]
        self.assertEqual(len(effective_measurements), 1)
        self.assertEqual(Decimal(effective_measurements[0]["value"]), Decimal("35.00"))

    def test_renewal_cycles_and_threshold_monitoring(self):
        """
        TS §12 / PRD US-08:
        - Quota resets (window change or usage drop) must be detected.
        - Reset must not be interpreted as negative usage (delta=NULL).
        - Threshold crossing (warning at >=80%, critical at >=95%).
        """
        # Snapshot 1: Normal usage 40% (200k / 500k)
        snap1 = {
            "quota_used": 200000,
            "quota_limit": 500000,
            "quota_percent_used": 40.0,
            "window": {"starts_at": "2026-10-01T00:00:00Z", "ends_at": "2026-10-31T23:59:59Z"},
        }
        res1 = self.collector.detect_quota_reset_or_renewal("bp01", snap1, None)
        self.assertFalse(res1["reset_detected"])
        self.assertEqual(res1["threshold_state"], "nominal")
        self.assertIsNone(res1["delta_used"])

        # Snapshot 2: Warning threshold reached (420k / 500k = 84%)
        snap2 = {
            "quota_used": 420000,
            "quota_limit": 500000,
            "quota_percent_used": 84.0,
            "window": {"starts_at": "2026-10-01T00:00:00Z", "ends_at": "2026-10-31T23:59:59Z"},
        }
        res2 = self.collector.detect_quota_reset_or_renewal("bp01", snap2, snap1)
        self.assertFalse(res2["reset_detected"])
        self.assertEqual(res2["threshold_state"], "warning")
        self.assertEqual(res2["delta_used"], 220000)

        # Snapshot 3: Critical threshold reached (485k / 500k = 97%)
        snap3 = {
            "quota_used": 485000,
            "quota_limit": 500000,
            "quota_percent_used": 97.0,
            "window": {"starts_at": "2026-10-01T00:00:00Z", "ends_at": "2026-10-31T23:59:59Z"},
        }
        res3 = self.collector.detect_quota_reset_or_renewal("bp01", snap3, snap2)
        self.assertFalse(res3["reset_detected"])
        self.assertEqual(res3["threshold_state"], "critical")
        self.assertEqual(res3["delta_used"], 65000)

        # Snapshot 4: Monthly renewal / reset! (window changed to November, usage resets to 15k)
        snap4 = {
            "quota_used": 15000,
            "quota_limit": 500000,
            "quota_percent_used": 3.0,
            "window": {"starts_at": "2026-11-01T00:00:00Z", "ends_at": "2026-11-30T23:59:59Z"},
        }
        res4 = self.collector.detect_quota_reset_or_renewal("bp01", snap4, snap3)
        self.assertTrue(res4["reset_detected"], "Reset must be detected on window shift")
        self.assertEqual(res4["reset_type"], "window_shift")
        self.assertEqual(res4["threshold_state"], "nominal")
        # Invariant: Must NOT be -470000 negative consumption!
        self.assertIsNone(res4["delta_used"], "Reset delta must be None, not negative usage")

    def test_compare_historical_snapshots_across_all_five_accounts(self):
        """
        Historische Snapshot-Vergleiche für bp01..bp05:
        Feeds chronological series of snapshots for all 5 accounts and verifies independent
        tracking, reset identification, and delta computations.
        """
        accounts = ["bp01", "bp02", "bp03", "bp04", "bp05"]
        for idx, acct in enumerate(accounts):
            base_limit = 500000
            history = [
                {
                    "observed_at": "2026-10-01T10:00:00Z",
                    "quota_used": 10000 * (idx + 1),
                    "quota_limit": base_limit,
                    "window": {"starts_at": "2026-10-01T00:00:00Z", "ends_at": "2026-10-31T23:59:59Z"},
                },
                {
                    "observed_at": "2026-10-15T10:00:00Z",
                    "quota_used": 250000 * (idx + 1) // 5,
                    "quota_limit": base_limit,
                    "window": {"starts_at": "2026-10-01T00:00:00Z", "ends_at": "2026-10-31T23:59:59Z"},
                },
                {
                    "observed_at": "2026-10-25T10:00:00Z",
                    "quota_used": 450000,
                    "quota_limit": base_limit,
                    "quota_percent_used": 90.0,
                    "window": {"starts_at": "2026-10-01T00:00:00Z", "ends_at": "2026-10-31T23:59:59Z"},
                },
                {
                    "observed_at": "2026-11-01T08:00:00Z",
                    "quota_used": 5000,
                    "quota_limit": base_limit,
                    "quota_percent_used": 1.0,
                    "window": {"starts_at": "2026-11-01T00:00:00Z", "ends_at": "2026-11-30T23:59:59Z"},
                },
            ]

            comps = self.collector.compare_historical_snapshots(acct, history)
            self.assertEqual(len(comps), 4)
            # The 3rd entry (90%) must be in warning state
            self.assertEqual(comps[2]["threshold_state"], "warning")
            # The 4th entry must report reset
            self.assertTrue(comps[3]["reset_detected"])
            self.assertIsNone(comps[3]["delta_used"])
            self.assertEqual(comps[3]["account_alias"], acct)


if __name__ == "__main__":
    unittest.main()
