"""
Unit and integration tests for storage/revisions:
- US-04.a: Korrektur erzeugt neue Revision mit Grund und Bezug zur vorigen; Standardberichte zählen nur die wirksame Revision.
- US-04.b: Ansicht „Datenstand vor Korrektur“ reproduziert vorherigen Wert; Schleifen in Korrekturbezügen werden abgelehnt.
- US-04.c: Re-Normalisierung mit neuer Mapping-Version überschreibt weder Beleg noch alten abgeleiteten Stand; aktiviert neuen nachvollziehbaren Stand.
- Auditierbarkeit, Actor-Bindung, Belegbindung, Verbot von Überschreiben (Historien-Erhaltung).
"""

import hashlib
import json
import os
import tempfile
import unittest
from decimal import Decimal

from storage.revisions import (
    DanglingRevisionError,
    ImmutableHistoryError,
    LineageInfo,
    MeasurementRevisionRecord,
    RevisionCycleError,
    RevisionRecord,
    RevisionStore,
    ValidationError,
    check_revision_chain,
    generate_ulid,
)


def make_sample_measurement(
    measurement_id: str,
    event_id: str,
    metric: str = "cost",
    value: str = "12.50",
    unit: str = "usd",
    source: str = "byteplus",
    source_account_id: str = "bp01",
    evidence_id: str = "ev_01J9Z3K5Q8W2X4Y6A7B8C9D0AA",
    recorded_at: str = "2026-10-26T01:00:00Z",
) -> MeasurementRevisionRecord:
    rev = RevisionRecord(
        revision_id=generate_ulid(),
        revision_no=1,
        supersedes=None,
        reason_code="initial",
        reason="initial capture from provider",
        recorded_at=recorded_at,
        effective=True,
        lineage=LineageInfo(
            mapping_version="0.1.0",
            transformation_version="0.1.0",
            input_measurement_ids=[],
        ),
        actor="collector_cron",
    )
    return MeasurementRevisionRecord(
        schema_version="0.1",
        measurement_id=measurement_id,
        event_id=event_id,
        metric=metric,
        value=value,
        unit=unit,
        aggregation="interval_total",
        source=source,
        source_account_id=source_account_id,
        provenance={
            "class": "P0",
            "method": "api",
            "confidence": 1.0,
            "confidence_reason": "direct billing api snapshot",
            "validation_status": "semantically_checked",
        },
        evidence_ids=[evidence_id],
        collector_version="1.0.0",
        mapping_version="0.1.0",
        revision=rev,
    )


class TestRevisionsUS04(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "revisions.db")
        self.store = RevisionStore(self.db_path)

        # Register sample evidence
        self.ev_id = "ev_01J9Z3K5Q8W2X4Y6A7B8C9D0AA"
        self.raw_data = b'{"billing_id": "inv_123", "amount": 12.50}'
        self.store.register_evidence(
            evidence_id=self.ev_id,
            source="byteplus",
            source_account_id="bp01",
            raw_bytes=self.raw_data,
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_us04_a_correction_creates_new_revision_and_standard_report_counts_only_effective(self):
        """
        US-04.a:
        - Eine Korrektur erzeugt eine neue Revision mit Grund und Bezug zur vorigen.
        - Standardberichte zählen nur die wirksame Revision.
        """
        mid = generate_ulid()
        eid = generate_ulid()
        meas = make_sample_measurement(mid, eid, value="10.00", evidence_id=self.ev_id)
        self.store.record_initial_measurement(meas, actor="cron_agent")

        initial_rev_id = meas.revision.revision_id

        # Initial check
        rep_init = self.store.generate_standard_report(metric="cost")
        self.assertEqual(rep_init["total_effective_measurements"], 1)
        self.assertEqual(rep_init["total_revisions_stored"], 1)
        self.assertEqual(rep_init["totals"]["cost"]["usd"], "10.00")

        # Apply correction (Ablesefehler: user corrected value from 10.00 to 15.00)
        corrected = self.store.correct_measurement(
            measurement_id=mid,
            new_value="15.00",
            reason_code="reading_error",
            reason="Ablesefehler: Kommastelle korrigiert",
            actor="analyst_alice",
            recorded_at="2026-10-26T02:00:00Z",
        )

        self.assertEqual(corrected.revision.revision_no, 2)
        self.assertEqual(corrected.revision.supersedes, initial_rev_id)
        self.assertEqual(corrected.revision.reason_code, "reading_error")
        self.assertEqual(corrected.revision.reason, "Ablesefehler: Kommastelle korrigiert")
        self.assertEqual(corrected.revision.actor, "analyst_alice")
        self.assertTrue(corrected.revision.effective)

        # Check history preservation
        history = self.store.get_revision_history(mid)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0].revision.revision_id, initial_rev_id)
        self.assertFalse(history[0].revision.effective)
        self.assertEqual(history[0].value, "10.00")
        self.assertEqual(history[1].revision.revision_id, corrected.revision.revision_id)
        self.assertTrue(history[1].revision.effective)
        self.assertEqual(history[1].value, "15.00")

        # Standard report: must count only effective revision
        rep_after = self.store.generate_standard_report(metric="cost")
        self.assertEqual(rep_after["total_effective_measurements"], 1)
        self.assertEqual(rep_after["total_revisions_stored"], 2)
        self.assertEqual(rep_after["superseded_revisions_excluded"], 1)
        self.assertEqual(rep_after["totals"]["cost"]["usd"], "15.00")

    def test_us04_b_prior_state_view_and_loop_rejection(self):
        """
        US-04.b:
        - Die Ansicht „Datenstand vor Korrektur“ reproduziert den vorherigen Wert.
        - Schleifen in Korrekturbezügen werden abgelehnt.
        """
        mid = generate_ulid()
        eid = generate_ulid()
        meas = make_sample_measurement(mid, eid, value="100", unit="token", metric="input_tokens", evidence_id=self.ev_id)
        self.store.record_initial_measurement(meas)

        # Correction 1: 100 -> 120
        corr1 = self.store.correct_measurement(
            measurement_id=mid,
            new_value=120,
            reason_code="provider_correction",
            reason="Provider rebate adjustment",
            actor="billing_admin",
            recorded_at="2026-10-26T01:30:00Z",
        )

        # Correction 2: 120 -> 125
        corr2 = self.store.correct_measurement(
            measurement_id=mid,
            new_value=125,
            reason_code="reading_error",
            reason="Refined token count",
            actor="billing_admin",
            recorded_at="2026-10-26T02:00:00Z",
        )

        # Ansicht "Datenstand vor Korrektur" for corr2 -> must yield corr1 (120)
        prior_to_corr2 = self.store.get_measurement_prior_to_revision(mid, corr2.revision.revision_id)
        self.assertIsNotNone(prior_to_corr2)
        self.assertEqual(prior_to_corr2.value, 120)
        self.assertEqual(prior_to_corr2.revision.revision_id, corr1.revision.revision_id)

        # Ansicht "Datenstand vor Korrektur" for corr1 -> must yield initial (100)
        prior_to_corr1 = self.store.get_measurement_prior_to_revision(mid, corr1.revision.revision_id)
        self.assertIsNotNone(prior_to_corr1)
        self.assertEqual(prior_to_corr1.value, "100")
        self.assertEqual(prior_to_corr1.revision.revision_id, meas.revision.revision_id)

        # Time-travel as_of views
        as_of_initial = self.store.get_measurement_as_of(mid, "2026-10-26T01:10:00Z")
        self.assertIsNotNone(as_of_initial)
        self.assertEqual(as_of_initial.value, "100")

        as_of_corr1 = self.store.get_measurement_as_of(mid, "2026-10-26T01:45:00Z")
        self.assertIsNotNone(as_of_corr1)
        self.assertEqual(as_of_corr1.value, 120)

        as_of_corr2 = self.store.get_measurement_as_of(mid, "2026-10-26T02:15:00Z")
        self.assertIsNotNone(as_of_corr2)
        self.assertEqual(as_of_corr2.value, 125)

        # Negative check: Schleifen in Korrekturbezügen werden abgelehnt
        revA_id = generate_ulid()
        revB_id = generate_ulid()
        cycle_revs = [
            RevisionRecord(
                revision_id=revA_id,
                revision_no=2,
                supersedes=revB_id,
                reason_code="reading_error",
                reason="Cycle step 1",
                recorded_at="2026-10-26T01:00:00Z",
                effective=False,
                lineage=LineageInfo("0.1.0", "0.1.0"),
            ),
            RevisionRecord(
                revision_id=revB_id,
                revision_no=3,
                supersedes=revA_id,
                reason_code="reading_error",
                reason="Cycle step 2",
                recorded_at="2026-10-26T01:00:00Z",
                effective=True,
                lineage=LineageInfo("0.1.0", "0.1.0"),
            ),
        ]
        errors = check_revision_chain(cycle_revs)
        self.assertTrue(any("cycle" in e for e in errors), f"Expected cycle error, got: {errors}")

    def test_us04_c_renormalization_preserves_evidence_and_derived_history(self):
        """
        US-04.c:
        - Re-Normalisierung mit neuer Mapping-Version überschreibt weder den erfassten Beleg
          noch den alten abgeleiteten Stand.
        - Sie aktiviert einen nachvollziehbaren neuen Stand.
        """
        mid = generate_ulid()
        eid = generate_ulid()
        meas = make_sample_measurement(mid, eid, value="100.00", evidence_id=self.ev_id)
        self.store.record_initial_measurement(meas)

        initial_state = self.store.get_effective_measurement(mid)
        self.assertEqual(initial_state.mapping_version, "0.1.0")
        self.assertEqual(initial_state.value, "100.00")

        # Execute re-normalization with mapping_version 0.2.0 (e.g. updated pricing or currency formula)
        renorm = self.store.renormalize_measurement(
            measurement_id=mid,
            new_mapping_version="0.2.0",
            new_value="112.50",
            reason="Updated provider cost formula in catalog v0.2.0",
            actor="mapping_engine_daemon",
            recorded_at="2026-10-26T03:00:00Z",
        )

        self.assertEqual(renorm.mapping_version, "0.2.0")
        self.assertEqual(renorm.value, "112.50")
        self.assertEqual(renorm.revision.reason_code, "renormalization")
        self.assertTrue(renorm.revision.effective)
        self.assertEqual(renorm.revision.supersedes, initial_state.revision.revision_id)

        # Verify old derived state is NOT overwritten
        old_derived = self.store.get_measurement_prior_to_revision(mid, renorm.revision.revision_id)
        self.assertEqual(old_derived.value, "100.00")
        self.assertEqual(old_derived.mapping_version, "0.1.0")

        # Verify underlying evidence descriptor and raw bytes are NOT overwritten
        with self.store._get_connection() as conn:
            ev_row = conn.execute(
                "SELECT raw_data, sha256 FROM evidence_descriptors WHERE evidence_id = ?",
                (self.ev_id,),
            ).fetchone()
            self.assertEqual(ev_row["raw_data"], self.raw_data)
            self.assertEqual(ev_row["sha256"], hashlib.sha256(self.raw_data).hexdigest())

        # Effective measurement in store is now the new normalized state
        curr = self.store.get_effective_measurement(mid)
        self.assertEqual(curr.mapping_version, "0.2.0")
        self.assertEqual(curr.value, "112.50")

    def test_history_preservation_and_direct_overwrite_forbidden(self):
        """
        Preservation of history: Overwriting measurements directly is prohibited.
        """
        mid = generate_ulid()
        eid = generate_ulid()
        meas = make_sample_measurement(mid, eid, value="50.00", evidence_id=self.ev_id)
        self.store.record_initial_measurement(meas)

        # Attempting direct overwrite must raise ImmutableHistoryError
        with self.assertRaises(ImmutableHistoryError):
            self.store.attempt_direct_overwrite(mid, "999.00")

        # Attempting to re-insert same measurement_id as initial must raise ImmutableHistoryError
        with self.assertRaises(ImmutableHistoryError):
            self.store.record_initial_measurement(meas)

    def test_negative_probes_revision_chain(self):
        """
        Negative probes:
        - Dangling supersedes link rejected
        - Multiple effective revisions rejected
        - Effective revision superseded rejected
        - Invalid actor or reason rejected
        - Evidence scope mismatch rejected
        """
        mid = generate_ulid()
        eid = generate_ulid()
        meas = make_sample_measurement(mid, eid, evidence_id=self.ev_id)
        self.store.record_initial_measurement(meas)

        # Dangling pointer test
        dangling_revs = [
            RevisionRecord(
                revision_id=generate_ulid(),
                revision_no=1,
                supersedes=None,
                reason_code="initial",
                reason="init",
                recorded_at="2026-10-26T01:00:00Z",
                effective=False,
                lineage=LineageInfo("0.1.0", "0.1.0"),
            ),
            RevisionRecord(
                revision_id=generate_ulid(),
                revision_no=2,
                supersedes=generate_ulid(),  # dangling non-existent id
                reason_code="reading_error",
                reason="corr",
                recorded_at="2026-10-26T01:00:00Z",
                effective=True,
                lineage=LineageInfo("0.1.0", "0.1.0"),
            ),
        ]
        errors = check_revision_chain(dangling_revs)
        self.assertTrue(any("dangling" in e for e in errors))

        # Multiple effective test
        two_effective = [
            RevisionRecord(
                revision_id=generate_ulid(),
                revision_no=1,
                supersedes=None,
                reason_code="initial",
                reason="init",
                recorded_at="2026-10-26T01:00:00Z",
                effective=True,
                lineage=LineageInfo("0.1.0", "0.1.0"),
            ),
            RevisionRecord(
                revision_id=generate_ulid(),
                revision_no=2,
                supersedes=None,  # will be filled
                reason_code="reading_error",
                reason="corr",
                recorded_at="2026-10-26T01:00:00Z",
                effective=True,
                lineage=LineageInfo("0.1.0", "0.1.0"),
            ),
        ]
        two_effective[1].supersedes = two_effective[0].revision_id
        errors = check_revision_chain(two_effective)
        self.assertTrue(any("exactly one effective" in e for e in errors))

        # Blank actor / reason rejected
        with self.assertRaises(ValidationError):
            self.store.correct_measurement(
                mid, "20.00", "reading_error", "", "actor1"
            )
        with self.assertRaises(ValidationError):
            self.store.correct_measurement(
                mid, "20.00", "reading_error", "valid reason", ""
            )

        # Cross-account evidence scope mismatch rejected
        foreign_ev_id = "ev_01J9Z3K5Q8W2X4Y6A7B8C9D0FF"
        self.store.register_evidence(
            foreign_ev_id,
            source="deepseek",
            source_account_id="ds_other",
            raw_bytes=b"cross account evidence",
        )
        with self.assertRaises(ValidationError):
            self.store.correct_measurement(
                mid, "20.00", "reading_error", "reason", "actor", evidence_ids=[foreign_ev_id]
            )


if __name__ == "__main__":
    unittest.main()
