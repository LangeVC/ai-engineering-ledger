#!/usr/bin/env python3
"""Comprehensive unit and scenario tests for collectors.backfill.
Covers:
- US-06.a: Repeated import of same file / repeated backfill of same provider IDs
           results in 0 new active measurements and unchanged sums. Replay attempts
           are recorded in the audit trail.
- US-06.b: Resumption after simulated interrupt between pagination pages.
           Resulting data matches an uninterrupted run exactly.
           Progress marker never exceeds the last securely committed checkpoint.
- US-06.c: Section 9 rules:
           * Identical artifact deduplication per source/account
           * Revisions for changed values on same scope/native_id
           * Equal timestamps and token counts alone do NOT merge without reliable IDs
- US-06.d: Requested, delivered, and confirmed periods documented separately.
           Empty response or oldest record seen does not prove full history.
"""

from __future__ import annotations

import decimal
from decimal import Decimal
import os
import sys
from pathlib import Path
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from collectors.backfill import (
    BackfillBatchResult,
    BackfillManager,
    BackfillStore,
    CoverageLedger,
    CoverageReport,
    CursorPaginationState,
    IdempotencyManager,
    PeriodCoverage,
    compute_sha256,
    derive_idempotency_key,
    generate_ulid,
)


class TestBackfillUnitAndScenarios(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_backfill.db"
        self.store = BackfillStore(db_path=self.db_path)
        self.manager = BackfillManager(store=self.store)

    def tearDown(self):
        self.store.close()
        self.temp_dir.cleanup()

    def test_us06_a_idempotent_replay_zero_new_active_and_unchanged_sums(self):
        """US-06.a: Zweiter Import derselben Datei und zweiter Backfill derselben Provider-IDs
        ändern aktive Anzahl und Summen nicht. Weitere Erfassungsversuche bleiben protokollierbar.
        """
        source = "deepseek"
        account = "ds-acc-01"
        backfill_id = "bf-us06a-01"

        # 1. First run with 3 records
        def page_fetcher(page: int, cursor: str | None):
            if page == 1:
                records = [
                    {"id": "req_001", "value": 100, "metric": "requests", "unit": "request"},
                    {"id": "req_002", "value": 250, "metric": "requests", "unit": "request"},
                    {"id": "req_003", "value": 150, "metric": "requests", "unit": "request"},
                ]
                return records, "cur_page_2", True
            elif page == 2:
                records = [
                    {"id": "req_004", "value": 500, "metric": "requests", "unit": "request"},
                ]
                return records, None, False
            return [], None, False

        run1 = self.manager.run_paginated_backfill(
            backfill_id=backfill_id,
            source=source,
            account=account,
            page_fetcher=page_fetcher,
            requested_start="2026-10-01T00:00:00Z",
            requested_end="2026-10-02T00:00:00Z",
        )

        self.assertEqual(run1.status, "complete")
        self.assertEqual(run1.accepted, 4)
        self.assertEqual(run1.new_active_measurements, 4)
        self.assertEqual(run1.active_sums["requests"], Decimal("1000"))
        self.assertTrue(run1.verify_disjoint_counts())

        # Check in DB directly
        count1, sums1 = self.store.get_active_measurements_count_and_sums(source=source, account=account)
        self.assertEqual(count1, 4)
        self.assertEqual(sums1["requests"], Decimal("1000"))

        # 2. Second run: replay same backfill / same provider IDs
        run2 = self.manager.run_paginated_backfill(
            backfill_id="bf-us06a-replay-02",
            source=source,
            account=account,
            page_fetcher=page_fetcher,
            requested_start="2026-10-01T00:00:00Z",
            requested_end="2026-10-02T00:00:00Z",
        )

        # Invariant: 0 new active measurements, unchanged active count and sums
        self.assertEqual(run2.accepted, 0)
        self.assertEqual(run2.new_active_measurements, 0)
        self.assertEqual(run2.existing, 4)
        self.assertTrue(run2.verify_disjoint_counts())

        count2, sums2 = self.store.get_active_measurements_count_and_sums(source=source, account=account)
        self.assertEqual(count2, 4)
        self.assertEqual(sums2["requests"], Decimal("1000"))

        # Further attempts remain logged and auditable in audit_attempts
        audit_log = self.store.get_audit_attempts()
        replay_attempts = [a for a in audit_log if a["attempt_type"] == "duplicate_replay"]
        self.assertEqual(len(replay_attempts), 4)

    def test_us06_a_file_artifact_duplicate_replay_check(self):
        """US-06.a: File import duplicate detection by artifact content hash.
        Second import of same file leaves active measurements untouched, logs attempt.
        """
        source = "deepseek"
        account = "account-1"
        file_content = "date,model,prompt_tokens,completion_tokens,cost\n2026-10-25,deepseek-chat,100,200,0.001\n"
        content_hash = compute_sha256(file_content)

        # First import
        is_dup1, count1 = self.store.check_or_record_artifact(source, account, content_hash, "export_file_1.csv")
        self.assertFalse(is_dup1)
        self.assertEqual(count1, 1)

        # Second import of identical file
        is_dup2, count2 = self.store.check_or_record_artifact(source, account, content_hash, "export_file_1.csv")
        self.assertTrue(is_dup2)
        self.assertEqual(count2, 2)

        # Third import attempt
        is_dup3, count3 = self.store.check_or_record_artifact(source, account, content_hash, "export_file_1.csv")
        self.assertTrue(is_dup3)
        self.assertEqual(count3, 3)

        audit_log = self.store.get_audit_attempts()
        artifact_audits = [a for a in audit_log if "Artifact re-imported" in a["details"]]
        self.assertEqual(len(artifact_audits), 2)

    def test_us06_b_interrupted_run_resumption_and_exact_match(self):
        """US-06.b: Nach simuliertem Abbruch zwischen zwei Seiten kann der Lauf fortgesetzt werden;
        Ergebnis entspricht einem ununterbrochenen Lauf. Kein Fortschrittsmarker liegt hinter dem
        letzten sicher gespeicherten Abschnitt.
        """
        source = "kilo"
        account = "kilo-test-acc"
        backfill_id_interrupted = "bf-us06b-interrupted"
        backfill_id_continuous = "bf-us06b-continuous"

        # Multi-page fixture: 3 pages, each with distinct records
        pages_data = {
            1: (
                [
                    {"id": "k1", "value": 100, "metric": "requests", "unit": "request"},
                    {"id": "k2", "value": 200, "metric": "requests", "unit": "request"},
                ],
                "cursor_p2",
                True,
            ),
            2: (
                [
                    {"id": "k3", "value": 300, "metric": "requests", "unit": "request"},
                    {"id": "k4", "value": 400, "metric": "requests", "unit": "request"},
                ],
                "cursor_p3",
                True,
            ),
            3: (
                [
                    {"id": "k5", "value": 500, "metric": "requests", "unit": "request"},
                ],
                None,
                False,
            ),
        }

        def fetcher(page: int, cursor: str | None):
            return pages_data[page]

        # 1. Uninterrupted baseline run in separate clean manager
        clean_store = BackfillStore(db_path=":memory:")
        clean_mgr = BackfillManager(store=clean_store)
        res_continuous = clean_mgr.run_paginated_backfill(
            backfill_id=backfill_id_continuous,
            source=source,
            account=account,
            page_fetcher=fetcher,
            requested_start="2026-10-01T00:00:00Z",
            requested_end="2026-10-03T00:00:00Z",
        )
        self.assertEqual(res_continuous.status, "complete")
        self.assertEqual(res_continuous.accepted, 5)
        self.assertEqual(res_continuous.active_sums["requests"], Decimal("1500"))

        # 2. Interrupted run in self.manager: simulate disconnect after page 1
        res_interrupted = self.manager.run_paginated_backfill(
            backfill_id=backfill_id_interrupted,
            source=source,
            account=account,
            page_fetcher=fetcher,
            requested_start="2026-10-01T00:00:00Z",
            requested_end="2026-10-03T00:00:00Z",
            simulate_interrupt_after_page=1,
        )

        self.assertEqual(res_interrupted.status, "interrupted")
        self.assertEqual(res_interrupted.committed_page, 1)
        self.assertEqual(res_interrupted.cursor, "cursor_p2")
        self.assertEqual(res_interrupted.accepted, 2)

        # Verify progress marker guarantee: checkpoint cursor is strictly at page 1
        chk = self.store.get_checkpoint(backfill_id_interrupted)
        self.assertIsNotNone(chk)
        self.assertEqual(chk.committed_page, 1)
        self.assertEqual(chk.committed_cursor, "cursor_p2")
        self.assertFalse(chk.is_finished)

        # 3. Resume the interrupted run
        res_resumed = self.manager.run_paginated_backfill(
            backfill_id=backfill_id_interrupted,
            source=source,
            account=account,
            page_fetcher=fetcher,
            requested_start="2026-10-01T00:00:00Z",
            requested_end="2026-10-03T00:00:00Z",
        )

        self.assertEqual(res_resumed.status, "complete")
        self.assertEqual(res_resumed.committed_page, 3)
        # In resumed run: remaining 3 records accepted
        self.assertEqual(res_resumed.accepted, 3)

        # 4. Compare resulting data: exactly matches uninterrupted run!
        count_interrupted, sums_interrupted = self.store.get_active_measurements_count_and_sums(
            source=source, account=account
        )
        count_cont, sums_cont = clean_store.get_active_measurements_count_and_sums(
            source=source, account=account
        )
        self.assertEqual(count_interrupted, count_cont)
        self.assertEqual(sums_interrupted, sums_cont)
        self.assertEqual(sums_interrupted["requests"], Decimal("1500"))

        clean_store.close()

    def test_us06_c_section9_revisions_supersede_and_do_not_add_double_counts(self):
        """US-06.c: Überlappende Exporte und Revisionen werden nach Abschnitt 9 behandelt.
        Geänderte Werte zum selben Scope / Provider-ID sind Revisionen, keine zusätzlichen
        Verbrauchsereignisse.
        """
        source = "faigate"
        account = "faigate-prod"
        window_start = "2026-10-25T10:00:00Z"
        window_end = "2026-10-25T11:00:00Z"

        # 1. Initial measurement
        action1, m1 = self.store.record_measurement(
            measurement_id="meas_01",
            event_id="ev_01",
            metric="input_tokens",
            value=1000,
            unit="token",
            aggregation="interval_total",
            source=source,
            account=account,
            provider="faigate",
            model="claude-3-5-sonnet",
            native_id="sess_abc_01",
            window_start=window_start,
            window_end=window_end,
        )
        self.assertEqual(action1, "accepted")

        count1, sums1 = self.store.get_active_measurements_count_and_sums(source=source, account=account)
        self.assertEqual(count1, 1)
        self.assertEqual(sums1["input_tokens"], Decimal("1000"))

        # 2. Revised export with corrected / updated value (1200 tokens instead of 1000) for SAME native_id
        action2, m2 = self.store.record_measurement(
            measurement_id="meas_02",
            event_id="ev_02",
            metric="input_tokens",
            value=1200,
            unit="token",
            aggregation="interval_total",
            source=source,
            account=account,
            provider="faigate",
            model="claude-3-5-sonnet",
            native_id="sess_abc_01",
            window_start=window_start,
            window_end=window_end,
        )
        self.assertEqual(action2, "revision")

        # Invariant: exactly 1 active measurement (revision superseded prior value, not added to it!)
        count2, sums2 = self.store.get_active_measurements_count_and_sums(source=source, account=account)
        self.assertEqual(count2, 1)
        self.assertEqual(sums2["input_tokens"], Decimal("1200"))

        # Check audit trail for revision entry
        audits = self.store.get_audit_attempts()
        rev_audits = [a for a in audits if a["attempt_type"] == "revision"]
        self.assertEqual(len(rev_audits), 1)

    def test_us06_c_requests_without_reliable_identity_not_collapsed_by_time_and_tokens_alone(self):
        """US-06.c: Fehlen zuverlässige Identitäten, werden gleichartige Einzelrequests
        nicht allein anhand gleicher Zeit und Tokenzahl zusammengelegt.
        """
        source = "generic"
        account = "anon-requests"
        window_start = "2026-10-25T14:00:00Z"
        window_end = "2026-10-25T14:00:00Z"

        # Two distinct requests occurring at identical time and consuming identical tokens (500 tokens)
        # Without reliable native_ids (native_id=None), force_distinct_unidentified=True preserves both.
        action1, m1 = self.store.record_measurement(
            measurement_id="anon_m1",
            event_id="anon_ev1",
            metric="input_tokens",
            value=500,
            unit="token",
            aggregation="delta",
            source=source,
            account=account,
            provider="generic",
            model="gpt-4o",
            native_id=None,
            window_start=window_start,
            window_end=window_end,
            force_distinct_unidentified=True,
        )
        self.assertEqual(action1, "accepted")

        action2, m2 = self.store.record_measurement(
            measurement_id="anon_m2",
            event_id="anon_ev2",
            metric="input_tokens",
            value=500,
            unit="token",
            aggregation="delta",
            source=source,
            account=account,
            provider="generic",
            model="gpt-4o",
            native_id=None,
            window_start=window_start,
            window_end=window_end,
            force_distinct_unidentified=True,
        )
        self.assertEqual(action2, "accepted")

        # Invariant: Both requests are recorded independently; they are NOT collapsed
        count, sums = self.store.get_active_measurements_count_and_sums(source=source, account=account)
        self.assertEqual(count, 2)
        self.assertEqual(sums["input_tokens"], Decimal("1000"))

    def test_us06_d_requested_vs_delivered_vs_confirmed_coverage(self):
        """US-06.d: Angefragter, gelieferter und als vollständig bestätigter Zeitraum werden
        getrennt dokumentiert. Leere Antwort oder älteste gefundene Zeile beweisen keine
        vollständige Historie.
        """
        source = "deepseek"
        account = "ds-acc"
        backfill_id = "bf-cov-01"

        ledger = CoverageLedger(self.store)

        # 1. Record an empty response for a period
        ledger.record_period(
            backfill_id=backfill_id,
            source=source,
            account=account,
            period_start="2025-01-01T00:00:00Z",
            period_end="2025-01-10T00:00:00Z",
            status="empty_unverified",
            records_count=0,
            oldest_record_time=None,
            newest_record_time=None,
            confirmation_reason="Empty API response; provider retention cutoff unknown",
        )

        # Empty response assessment: empty response does NOT prove complete history!
        cov1 = self.manager.assess_coverage(
            source=source,
            account=account,
            requested_start="2025-01-01T00:00:00Z",
            requested_end="2025-01-10T00:00:00Z",
            backfill_id=backfill_id,
            provider_supports_full_history=False,
        )

        self.assertEqual(cov1.total_records_delivered, 0)
        self.assertTrue(cov1.empty_response_observed)
        self.assertFalse(cov1.is_fully_confirmed)
        self.assertEqual(len(cov1.confirmed_periods), 0)
        self.assertEqual(len(cov1.unconfirmed_gaps), 1)

        # 2. Record a delivered batch with oldest record found
        backfill_id2 = "bf-cov-02"
        ledger.record_period(
            backfill_id=backfill_id2,
            source=source,
            account=account,
            period_start="2026-01-01T00:00:00Z",
            period_end="2026-02-01T00:00:00Z",
            status="partial",
            records_count=50,
            oldest_record_time="2026-01-15T08:00:00Z",
            newest_record_time="2026-02-01T00:00:00Z",
            confirmation_reason="API returned records starting at 2026-01-15; pre-15th unconfirmed",
        )

        # Oldest record found (2026-01-15) does NOT prove history between 2026-01-01 and 2026-01-15
        cov2 = self.manager.assess_coverage(
            source=source,
            account=account,
            requested_start="2026-01-01T00:00:00Z",
            requested_end="2026-02-01T00:00:00Z",
            backfill_id=backfill_id2,
            provider_supports_full_history=False,
        )

        self.assertEqual(cov2.total_records_delivered, 50)
        self.assertEqual(cov2.oldest_record_seen, "2026-01-15T08:00:00Z")
        self.assertFalse(cov2.is_fully_confirmed)
        self.assertEqual(len(cov2.confirmed_periods), 0)
        self.assertEqual(len(cov2.unconfirmed_gaps), 1)

        # 3. Explicitly confirmed complete period
        backfill_id3 = "bf-cov-03"
        ledger.record_period(
            backfill_id=backfill_id3,
            source=source,
            account=account,
            period_start="2026-09-01T00:00:00Z",
            period_end="2026-10-01T00:00:00Z",
            status="confirmed_complete",
            records_count=120,
            oldest_record_time="2026-09-01T01:00:00Z",
            newest_record_time="2026-09-30T23:00:00Z",
            confirmation_reason="Full pagination exhausted within guaranteed provider retention window",
        )

        cov3 = self.manager.assess_coverage(
            source=source,
            account=account,
            requested_start="2026-09-01T00:00:00Z",
            requested_end="2026-10-01T00:00:00Z",
            backfill_id=backfill_id3,
            provider_supports_full_history=True,
        )
        self.assertTrue(cov3.is_fully_confirmed)
        self.assertEqual(len(cov3.confirmed_periods), 1)
        self.assertEqual(len(cov3.unconfirmed_gaps), 0)


if __name__ == "__main__":
    unittest.main()
