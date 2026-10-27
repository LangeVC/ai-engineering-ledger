"""
Unit and integration tests for storage/evidence.
Verifies PRD requirements US-03.a, US-03.b, US-03.c, DQ01, DQ02.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
import zipfile
from decimal import Decimal
from pathlib import Path

from storage.evidence import (
    EvidenceStore,
    EvidenceArtifact,
    Measurement,
    Provenance,
    RedactionInfo,
    ValidationError,
    IntegrityError,
    ImmutableStorageError,
    generate_evidence_id,
    generate_ulid,
    compute_sha256,
)


class TestEvidenceStore(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="ael_test_evidence_")
        self.store = EvidenceStore(base_dir=self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_us03_a_raw_payload_and_evidence_binding(self):
        """
        US-03.a:
        Jede akzeptierte Messung besitzt Source, Account/Scope, Metrik, Wert, Einheit,
        Aggregationssemantik, Zeitangabe, Provenanceklasse/-methode, Confidence,
        Collector-/Mapping-Version und mindestens eine auflösbare Evidenzreferenz.
        """
        raw_content = b'{"response": "ok", "usage": {"prompt_tokens": 120, "completion_tokens": 40}}'
        artifact = self.store.store_raw_payload(
            payload_bytes=raw_content,
            source="deepseek",
            account="primary",
            mime_type="application/json",
            collector_version="1.0.0",
            capture_point="provider_response_verbatim",
        )

        self.assertIsNotNone(artifact.evidence_id)
        self.assertTrue(artifact.evidence_id.startswith("ev_"))
        self.assertEqual(artifact.sha256, compute_sha256(raw_content))
        self.assertEqual(artifact.size_bytes, len(raw_content))
        self.assertFalse(artifact.redaction.redacted)
        self.assertIsNone(artifact.redaction.redaction_version)

        # File is on disk and read-only
        artifact_disk_path = Path(self.temp_dir) / artifact.stored_relpath
        self.assertTrue(artifact_disk_path.exists())
        self.assertEqual(artifact_disk_path.read_bytes(), raw_content)

        # Ingest valid measurement bound to this evidence
        prov = Provenance(
            provenance_class="P0",
            method="api",
            confidence=0.99,
            confidence_reason="Authoritative provider response JSON",
            validation_status="format_checked",
        )
        meas = Measurement(
            measurement_id=generate_ulid(),
            event_id=generate_ulid(),
            metric="input_tokens",
            value=120,
            unit="token",
            aggregation="interval_total",
            source="deepseek",
            source_account_id="primary",
            provenance=prov,
            evidence_ids=[artifact.evidence_id],
            collector_version="1.0.0",
            mapping_version="0.1.0",
        )

        report = self.store.ingest_measurements([meas])
        self.assertEqual(report.total_records, 1)
        self.assertEqual(report.accepted_records, 1)
        self.assertEqual(report.rejected_records, 0)
        self.assertEqual(len(report.rejections), 0)

        # Verify active measurement view
        active = self.store.get_active_measurements(metric="input_tokens")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].value, Decimal("120"))
        self.assertEqual(active[0].source, "deepseek")
        self.assertEqual(active[0].source_account_id, "primary")
        self.assertEqual(active[0].provenance.provenance_class, "P0")
        self.assertEqual(active[0].collector_version, "1.0.0")
        self.assertEqual(active[0].mapping_version, "0.1.0")
        self.assertEqual(active[0].evidence_ids, [artifact.evidence_id])

        # Trace back to evidence
        trace = self.store.trace_number_to_evidence(meas.measurement_id)
        self.assertEqual(trace["audit_status"], "verified")
        self.assertEqual(trace["evidence_count"], 1)
        self.assertEqual(trace["evidences"][0]["artifact"]["evidence_id"], artifact.evidence_id)
        self.assertEqual(trace["evidences"][0]["verification"]["verified"], True)

    def test_us03_a_unresolvable_evidence_quarantine(self):
        """
        US-03.a & DQ01:
        Messungen mit nicht auflösbaren Evidenzreferenzen werden abgewiesen und landen im Importbericht.
        """
        prov = Provenance(
            provenance_class="P0",
            method="api",
            confidence=0.95,
            confidence_reason="Simulated",
            validation_status="unvalidated",
        )
        fake_ev_id = "ev_01HZX9Y8B7C6D5E4F3G2H1J000"
        meas = Measurement(
            measurement_id=generate_ulid(),
            event_id=generate_ulid(),
            metric="requests",
            value=5,
            unit="request",
            aggregation="interval_total",
            source="deepseek",
            source_account_id="primary",
            provenance=prov,
            evidence_ids=[fake_ev_id],
            collector_version="1.0.0",
            mapping_version="0.1.0",
        )

        report = self.store.ingest_measurements([meas])
        self.assertEqual(report.total_records, 1)
        self.assertEqual(report.accepted_records, 0)
        self.assertEqual(report.rejected_records, 1)
        self.assertIn("Unresolvable evidence reference", report.rejections[0].reason)
        self.assertEqual(report.rejections[0].field_name, "evidence_ids")
        self.assertEqual(report.rejections[0].line_number, 1)

        # Ensure not in active view
        active = self.store.get_active_measurements(metric="requests")
        self.assertEqual(len(active), 0)

    def test_us03_b_validation_missing_fields_and_invalid_numbers(self):
        """
        US-03.b:
        Fehlende Pflichtfelder, ungültige Prozentwerte und nicht endliche Zahlen
        werden vor Aufnahme in die aktive Sicht mit konkretem Grund abgewiesen;
        Originalfehler und Zeilenposition bleiben im Importbericht.
        """
        # Store a valid evidence to bind
        art = self.store.store_raw_payload(
            payload_bytes=b"sample",
            source="openai",
            account="team-a",
            mime_type="text/plain",
            collector_version="1.0.0",
        )

        # Batch of bad records:
        # Record 1: Missing metric
        rec1 = {
            "measurement_id": generate_ulid(),
            "event_id": generate_ulid(),
            "value": 10,
            "unit": "request",
            "aggregation": "absolute",
            "source": "openai",
            "source_account_id": "team-a",
            "collector_version": "1.0.0",
            "mapping_version": "0.1.0",
            "evidence_ids": [art.evidence_id],
            "provenance": {
                "class": "P0",
                "method": "api",
                "confidence": 1.0,
                "confidence_reason": "ok",
                "validation_status": "unvalidated",
            },
        }

        # Record 2: Invalid percent (> 100)
        rec2 = {
            "measurement_id": generate_ulid(),
            "event_id": generate_ulid(),
            "metric": "quota_percent_used",
            "value": "105.5",
            "unit": "percent",
            "aggregation": "gauge",
            "source": "openai",
            "source_account_id": "team-a",
            "collector_version": "1.0.0",
            "mapping_version": "0.1.0",
            "evidence_ids": [art.evidence_id],
            "provenance": {
                "class": "P0",
                "method": "api",
                "confidence": 1.0,
                "confidence_reason": "ok",
                "validation_status": "unvalidated",
            },
        }

        # Record 3: Invalid percent (< 0)
        rec3 = {
            "measurement_id": generate_ulid(),
            "event_id": generate_ulid(),
            "metric": "quota_percent_used",
            "value": "-5",
            "unit": "percent",
            "aggregation": "gauge",
            "source": "openai",
            "source_account_id": "team-a",
            "collector_version": "1.0.0",
            "mapping_version": "0.1.0",
            "evidence_ids": [art.evidence_id],
            "provenance": {
                "class": "P0",
                "method": "api",
                "confidence": 1.0,
                "confidence_reason": "ok",
                "validation_status": "unvalidated",
            },
        }

        # Record 4: Non-finite float string (NaN / Infinity)
        rec4 = {
            "measurement_id": generate_ulid(),
            "event_id": generate_ulid(),
            "metric": "cost",
            "value": "NaN",
            "unit": "usd",
            "aggregation": "delta",
            "source": "openai",
            "source_account_id": "team-a",
            "collector_version": "1.0.0",
            "mapping_version": "0.1.0",
            "evidence_ids": [art.evidence_id],
            "provenance": {
                "class": "P0",
                "method": "api",
                "confidence": 1.0,
                "confidence_reason": "ok",
                "validation_status": "unvalidated",
            },
        }

        # Record 5: Python float (forbidden by DQ04 / decimal rule)
        rec5 = {
            "measurement_id": generate_ulid(),
            "event_id": generate_ulid(),
            "metric": "cost",
            "value": 12.34,  # float
            "unit": "usd",
            "aggregation": "delta",
            "source": "openai",
            "source_account_id": "team-a",
            "collector_version": "1.0.0",
            "mapping_version": "0.1.0",
            "evidence_ids": [art.evidence_id],
            "provenance": {
                "class": "P0",
                "method": "api",
                "confidence": 1.0,
                "confidence_reason": "ok",
                "validation_status": "unvalidated",
            },
        }

        # Record 6: Valid record
        rec6 = {
            "measurement_id": generate_ulid(),
            "event_id": generate_ulid(),
            "metric": "cost",
            "value": "12.34",  # decimal string is valid
            "unit": "usd",
            "aggregation": "delta",
            "source": "openai",
            "source_account_id": "team-a",
            "collector_version": "1.0.0",
            "mapping_version": "0.1.0",
            "evidence_ids": [art.evidence_id],
            "provenance": {
                "class": "P0",
                "method": "api",
                "confidence": 1.0,
                "confidence_reason": "ok",
                "validation_status": "unvalidated",
            },
        }

        batch = [rec1, rec2, rec3, rec4, rec5, rec6]
        report = self.store.ingest_measurements(batch)

        self.assertEqual(report.total_records, 6)
        self.assertEqual(report.accepted_records, 1)
        self.assertEqual(report.rejected_records, 5)

        # Check line numbers and reasons
        self.assertEqual(report.rejections[0].line_number, 1)
        self.assertEqual(report.rejections[0].field_name, "metric")
        self.assertIn("Missing required field 'metric'", report.rejections[0].reason)

        self.assertEqual(report.rejections[1].line_number, 2)
        self.assertEqual(report.rejections[1].field_name, "value")
        self.assertIn("Invalid percentage value", report.rejections[1].reason)

        self.assertEqual(report.rejections[2].line_number, 3)
        self.assertEqual(report.rejections[2].field_name, "value")
        self.assertIn("Invalid percentage value", report.rejections[2].reason)

        self.assertEqual(report.rejections[3].line_number, 4)
        self.assertEqual(report.rejections[3].field_name, "value")
        self.assertIn("Non-finite numeric value", report.rejections[3].reason)

        self.assertEqual(report.rejections[4].line_number, 5)
        self.assertEqual(report.rejections[4].field_name, "value")
        self.assertIn("Float value forbidden", report.rejections[4].reason)

        # Active view contains only rec6
        active = self.store.get_active_measurements(metric="cost")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].value, Decimal("12.34"))

    def test_us03_c_sha256_integrity_and_single_byte_tampering(self):
        """
        US-03.c:
        Hashprüfung eines gespeicherten Artefakts besteht; nach Änderung eines Bytes
        meldet die Prüfung eine Integritätsverletzung. Abgeleitete Berichte markieren
        den betroffenen Beleg als nicht verifiziert.
        """
        original_data = b'{"response": "authoritative data from deepseek", "tokens": 500}'
        art = self.store.store_raw_payload(
            payload_bytes=original_data,
            source="deepseek",
            account="main",
            mime_type="application/json",
            collector_version="1.0.0",
        )

        # 1. Unmodified check passes
        ver_initial = self.store.verify_evidence(art.evidence_id)
        self.assertTrue(ver_initial.verified)
        self.assertEqual(ver_initial.actual_sha256, art.sha256)
        self.assertIsNone(ver_initial.error)

        # Ingest a measurement bound to this evidence
        prov = Provenance(
            provenance_class="P0",
            method="api",
            confidence=1.0,
            confidence_reason="verified payload",
            validation_status="format_checked",
        )
        meas = Measurement(
            measurement_id=generate_ulid(),
            event_id=generate_ulid(),
            metric="total_tokens",
            value=500,
            unit="token",
            aggregation="interval_total",
            source="deepseek",
            source_account_id="main",
            provenance=prov,
            evidence_ids=[art.evidence_id],
            collector_version="1.0.0",
            mapping_version="0.1.0",
        )
        self.store.ingest_measurements([meas])

        # Initial derived report is verified
        rep_initial = self.store.generate_derived_report("total_tokens")
        self.assertEqual(rep_initial["report_verification_status"], "verified")
        self.assertFalse(rep_initial["has_integrity_breach"])
        self.assertEqual(rep_initial["contributing_measurements"][0]["audit_status"], "verified")

        # 2. Tamper exactly ONE byte in the stored file on disk
        target_path = Path(self.temp_dir) / art.stored_relpath
        os.chmod(target_path, 0o644)  # temporarily grant write permissions
        content = bytearray(target_path.read_bytes())
        # Flip first character '{' -> '['
        content[0] = ord(b"[")
        target_path.write_bytes(bytes(content))
        os.chmod(target_path, 0o444)

        # 3. Direct verification of evidence reports integrity failure
        ver_tampered = self.store.verify_evidence(art.evidence_id)
        self.assertFalse(ver_tampered.verified)
        self.assertNotEqual(ver_tampered.actual_sha256, art.sha256)
        self.assertIn("SHA256 mismatch", ver_tampered.error)

        # Reading bytes raises IntegrityError
        with self.assertRaises(IntegrityError):
            self.store.read_evidence_bytes(art.evidence_id)

        # 4. Tracing from measurement marks receipt as unverified
        trace_tampered = self.store.trace_number_to_evidence(meas.measurement_id)
        self.assertEqual(trace_tampered["audit_status"], "unverified")
        self.assertTrue(trace_tampered["integrity_violation"])

        # 5. Derived report marks overall aggregate and affected receipt as unverified
        rep_tampered = self.store.generate_derived_report("total_tokens")
        self.assertEqual(rep_tampered["report_verification_status"], "unverified")
        self.assertTrue(rep_tampered["has_integrity_breach"])
        self.assertEqual(
            rep_tampered["contributing_measurements"][0]["audit_status"],
            "unverified"
        )

    def test_us03_c_redaction_versioning_and_privacy_safeguards(self):
        """
        US-03.c & PRD 10.3 / schema:
        Redaktionsversionierung und Nachweisbarkeit:
        - Redigierte Belege müssen capture_point != provider_response_verbatim haben.
        - Redigierte Belege müssen redaction_version besitzen.
        - Das Original mit Secrets darf nicht archiviert werden (original_sha256 verboten).
        - Hashprüfung bezieht sich strikt auf die post-redaction gespeicherten Bytes.
        """
        clean_text = b'{"account": "org_***redacted***", "spend_usd": "42.00"}'
        redaction = RedactionInfo(
            redacted=True,
            redaction_version="regex_v1.0",
            redaction_reason="Strips personal bearer tokens and client secrets",
        )

        # 1. Validation failure: redacted cannot have capture_point 'provider_response_verbatim'
        with self.assertRaises(ValidationError) as ctx:
            self.store.store_raw_payload(
                payload_bytes=clean_text,
                source="openai",
                account="org-safe",
                mime_type="application/json",
                collector_version="1.0.0",
                capture_point="provider_response_verbatim",
                redaction=redaction,
            )
        self.assertIn("cannot have capture_point 'provider_response_verbatim'", str(ctx.exception))

        # 2. Validation failure: forbidden original_sha256
        with self.assertRaises(ValidationError) as ctx:
            RedactionInfo.from_dict({
                "redacted": True,
                "redaction_version": "v1",
                "original_sha256": "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
            })
        self.assertIn("original_sha256 cannot be stored", str(ctx.exception))

        # 3. Valid redacted artifact storage with capture_point 'post_redaction'
        art = self.store.store_raw_payload(
            payload_bytes=clean_text,
            source="openai",
            account="org-safe",
            mime_type="application/json",
            collector_version="1.0.0",
            capture_point="post_redaction",
            redaction=redaction,
        )

        self.assertTrue(art.redaction.redacted)
        self.assertEqual(art.redaction.redaction_version, "regex_v1.0")
        self.assertEqual(art.sha256, compute_sha256(clean_text))

        # Check descriptor matches JSON schema
        desc = art.to_descriptor()
        self.assertEqual(desc["capture_point"], "post_redaction")
        self.assertTrue(desc["redaction"]["redacted"])
        self.assertEqual(desc["redaction"]["redaction_version"], "regex_v1.0")
        self.assertNotIn("original_sha256", desc["redaction"])

        # Verification succeeds
        ver = self.store.verify_evidence(art.evidence_id)
        self.assertTrue(ver.verified)
        self.assertTrue(ver.is_redacted)

    def test_dq02_verify_all_evidences_audit(self):
        """
        DQ02: Belegintegrität:
        100% der vorhandenen referenzierten Artefakte bestehen Hashprüfung.
        """
        art1 = self.store.store_raw_payload(b"hello 1", "deepseek", "acc1", "text/plain", "1.0.0")
        art2 = self.store.store_raw_payload(b"hello 2", "deepseek", "acc2", "text/plain", "1.0.0")

        results = self.store.verify_all_evidences()
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.verified for r in results))

    def test_us03_d_lineage_and_transformation_versions(self):
        """
        US-03.d:
        Aus einer Berichtszahl lassen sich sämtliche beitragenden aktiven Messungen
        und deren Transformationsversionen ermitteln.
        """
        raw_art = self.store.store_raw_payload(b'{"total": 5000}', "byteplus", "bp01", "application/json", "1.0.0")

        # Ingest two active measurements contributing to total_tokens
        prov = Provenance("P0", "api", 1.0, "direct ark api", "format_checked")
        m1 = Measurement(
            measurement_id=generate_ulid(),
            event_id=generate_ulid(),
            metric="total_tokens",
            value=Decimal("2000"),
            unit="token",
            aggregation="interval_total",
            source="byteplus",
            source_account_id="bp01",
            provenance=prov,
            evidence_ids=[raw_art.evidence_id],
            collector_version="1.2.0",
            mapping_version="0.1.0",
            revision={
                "revision_id": generate_ulid(),
                "revision_no": 1,
                "supersedes": None,
                "reason_code": "initial",
                "reason": "initial capture",
                "recorded_at": "2026-10-26T12:00:00Z",
                "effective": True,
                "lineage": {
                    "mapping_version": "0.1.0",
                    "transformation_version": "0.1.0",
                    "input_measurement_ids": [],
                },
            },
        )
        m2 = Measurement(
            measurement_id=generate_ulid(),
            event_id=generate_ulid(),
            metric="total_tokens",
            value=Decimal("3000"),
            unit="token",
            aggregation="interval_total",
            source="byteplus",
            source_account_id="bp01",
            provenance=prov,
            evidence_ids=[raw_art.evidence_id],
            collector_version="1.2.0",
            mapping_version="0.2.0",
            revision={
                "revision_id": generate_ulid(),
                "revision_no": 1,
                "supersedes": None,
                "reason_code": "initial",
                "reason": "initial capture with updated mapping",
                "recorded_at": "2026-10-26T12:05:00Z",
                "effective": True,
                "lineage": {
                    "mapping_version": "0.2.0",
                    "transformation_version": "0.1.1",
                    "input_measurement_ids": [],
                },
            },
        )
        report = self.store.ingest_measurements([m1, m2])
        self.assertEqual(report.accepted_records, 2)

        # Derived report aggregates the numbers
        derived = self.store.generate_derived_report("total_tokens")
        self.assertEqual(derived["total_value"], "5000")
        self.assertEqual(derived["count"], 2)
        self.assertEqual(derived["report_verification_status"], "verified")

        # Trace each contributing measurement
        contributing = derived["contributing_measurements"]
        self.assertEqual(len(contributing), 2)

        # Verify mapping and collector versions are explicitly exposed for external auditors
        m1_trace = next(c for c in contributing if c["measurement_id"] == m1.measurement_id)
        m2_trace = next(c for c in contributing if c["measurement_id"] == m2.measurement_id)

        self.assertEqual(m1_trace["collector_version"], "1.2.0")
        self.assertEqual(m1_trace["mapping_version"], "0.1.0")
        self.assertEqual(m1_trace["evidence_count"], 1)
        self.assertEqual(m1_trace["evidences"][0]["artifact"]["evidence_id"], raw_art.evidence_id)

        self.assertEqual(m2_trace["collector_version"], "1.2.0")
        self.assertEqual(m2_trace["mapping_version"], "0.2.0")

    def test_us03_d_export_evidence_bundle_and_verify_targz(self):
        """
        US-03.d:
        Exportierbares Evidenzbündel mit Prüfsummen für externe Prüfer
        sowie Verifikation von Belegbündeln gegen das Manifest (tar.gz).
        """
        payload1 = b'{"deepseek_call": 1, "tokens": 450}'
        payload2 = b'{"deepseek_call": 2, "tokens": 550}'
        art1 = self.store.store_raw_payload(payload1, "deepseek", "acc1", "application/json", "1.0.0")
        art2 = self.store.store_raw_payload(payload2, "deepseek", "acc1", "application/json", "1.0.0")

        prov = Provenance("P0", "api", 1.0, "direct", "format_checked")
        m1 = Measurement(
            measurement_id=generate_ulid(),
            event_id=generate_ulid(),
            metric="total_tokens",
            value=Decimal("450"),
            unit="token",
            aggregation="interval_total",
            source="deepseek",
            source_account_id="acc1",
            provenance=prov,
            evidence_ids=[art1.evidence_id],
            collector_version="1.0.0",
            mapping_version="0.1.0",
        )
        m2 = Measurement(
            measurement_id=generate_ulid(),
            event_id=generate_ulid(),
            metric="total_tokens",
            value=Decimal("550"),
            unit="token",
            aggregation="interval_total",
            source="deepseek",
            source_account_id="acc1",
            provenance=prov,
            evidence_ids=[art2.evidence_id],
            collector_version="1.0.0",
            mapping_version="0.1.0",
        )
        self.store.ingest_measurements([m1, m2])

        bundle_tar = Path(self.temp_dir) / "evidence_bundle.tar.gz"
        export_info = self.store.export_evidence_bundle(
            export_path=bundle_tar,
            bundle_format="tar.gz",
            transformation_version="0.1.0",
        )
        self.assertTrue(bundle_tar.exists())
        self.assertEqual(export_info["evidence_count"], 2)
        self.assertEqual(export_info["measurement_count"], 2)
        self.assertIn("bundle_sha256", export_info)
        self.assertIn("manifest_sha256", export_info)

        # External auditor verification of the bundle
        verify_result = EvidenceStore.verify_evidence_bundle(bundle_tar)
        self.assertTrue(verify_result["verified"])
        self.assertEqual(verify_result["audit_verdict"], "PASS")
        self.assertEqual(verify_result["total_evidences"], 2)
        self.assertEqual(verify_result["verified_evidences"], 2)
        self.assertEqual(verify_result["total_measurements"], 2)
        self.assertEqual(verify_result["verified_measurements"], 2)

    def test_us03_d_export_evidence_bundle_and_verify_zip(self):
        """
        US-03.d:
        Exportierbares Evidenzbündel im ZIP-Format und externe Verifikation.
        """
        payload = b"Sample raw log output for audit trail"
        art = self.store.store_raw_payload(payload, "kilo", "default", "text/plain", "1.0.0")
        prov = Provenance("P1", "local", 0.99, "kilo session log", "semantically_checked")
        m = Measurement(
            measurement_id=generate_ulid(),
            event_id=generate_ulid(),
            metric="requests",
            value=1,
            unit="request",
            aggregation="absolute",
            source="kilo",
            source_account_id="default",
            provenance=prov,
            evidence_ids=[art.evidence_id],
            collector_version="1.0.0",
            mapping_version="0.1.0",
        )
        self.store.ingest_measurements([m])

        bundle_zip = Path(self.temp_dir) / "evidence_bundle.zip"
        self.store.export_evidence_bundle(export_path=bundle_zip, bundle_format="zip")
        self.assertTrue(bundle_zip.exists())

        verify_result = EvidenceStore.verify_evidence_bundle(bundle_zip)
        self.assertTrue(verify_result["verified"])
        self.assertEqual(verify_result["audit_verdict"], "PASS")
        self.assertEqual(verify_result["verified_evidences"], 1)

    def test_us03_d_bundle_verification_negative_tampering(self):
        """
        Negative fachliche Probe für US-03.d:
        Manipulation einer Belegdatei im Bündelarchiv führt zu audit_verdict='FAIL',
        identifiziert die betroffene Datei und schlägt fehl.
        """
        payload = b'{"original_unmodified": true}'
        art = self.store.store_raw_payload(payload, "github", "org1", "application/json", "1.0.0")
        prov = Provenance("P0", "api", 1.0, "github api", "format_checked")
        m = Measurement(
            measurement_id=generate_ulid(),
            event_id=generate_ulid(),
            metric="requests",
            value=10,
            unit="request",
            aggregation="absolute",
            source="github",
            source_account_id="org1",
            provenance=prov,
            evidence_ids=[art.evidence_id],
            collector_version="1.0.0",
            mapping_version="0.1.0",
        )
        self.store.ingest_measurements([m])

        bundle_zip = Path(self.temp_dir) / "bundle_to_tamper.zip"
        self.store.export_evidence_bundle(export_path=bundle_zip, bundle_format="zip")

        # Unpack, tamper 1 byte of the evidence, and repack
        tamper_dir = Path(self.temp_dir) / "tamper_scratch"
        tamper_dir.mkdir()
        with zipfile.ZipFile(bundle_zip, "r") as zf:
            zf.extractall(tamper_dir)

        ev_target_file = tamper_dir / "evidence" / f"{art.evidence_id}.bin"
        self.assertTrue(ev_target_file.exists())
        # Tamper 1 byte
        content = ev_target_file.read_bytes()
        tampered_content = content[:-1] + b"X" if content[-1:] != b"X" else content[:-1] + b"Y"
        ev_target_file.write_bytes(tampered_content)

        # Repack tampered bundle
        tampered_bundle_zip = Path(self.temp_dir) / "tampered_bundle.zip"
        with zipfile.ZipFile(tampered_bundle_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(tamper_dir):
                for file in files:
                    full_p = Path(root) / file
                    rel_p = full_p.relative_to(tamper_dir)
                    zf.write(full_p, str(rel_p))

        # Run bundle verification on tampered archive
        ver = EvidenceStore.verify_evidence_bundle(tampered_bundle_zip)
        self.assertFalse(ver["verified"])
        self.assertEqual(ver["audit_verdict"], "FAIL")
        self.assertEqual(ver["verified_evidences"], 0)
        self.assertIn("SHA256 mismatch", ver["evidence_checks"][0]["error"])

    def test_us03_d_audit_report_generation(self):
        """
        US-03.d:
        Audit-Bericht mit Integritätsnachweis über alle Belege.
        Überprüft Gesamtintegrität und erkennt Integritätsverletzungen.
        """
        art1 = self.store.store_raw_payload(b"Payload A", "source1", "acc1", "text/plain", "1.0.0")
        art2 = self.store.store_raw_payload(b"Payload B", "source2", "acc2", "text/plain", "1.0.0")

        # Clean audit report
        audit = self.store.generate_audit_report()
        self.assertEqual(audit["audit_verdict"], "PASS")
        self.assertEqual(audit["integrity_summary"]["total_evidences"], 2)
        self.assertEqual(audit["integrity_summary"]["verified_evidences"], 2)
        self.assertEqual(audit["integrity_summary"]["breach_count"], 0)
        self.assertEqual(audit["integrity_summary"]["dq02_compliance_percent"], "100.00")
        self.assertEqual(len(audit["evidence_catalog"]), 2)

        # Tamper one artifact file in the store
        target_path = Path(self.store.base_dir) / art1.stored_relpath
        os.chmod(target_path, 0o666)
        target_path.write_bytes(b"Tampered Payload A")
        os.chmod(target_path, 0o444)

        # Audit report now reflects breach and audit_verdict='FAIL'
        audit_tampered = self.store.generate_audit_report()
        self.assertEqual(audit_tampered["audit_verdict"], "FAIL")
        self.assertEqual(audit_tampered["integrity_summary"]["verified_evidences"], 1)
        self.assertEqual(audit_tampered["integrity_summary"]["breach_count"], 1)
        self.assertEqual(audit_tampered["integrity_summary"]["dq02_compliance_percent"], "50.00")
        self.assertEqual(len(audit_tampered["integrity_breaches"]), 1)
        self.assertEqual(audit_tampered["integrity_breaches"][0]["evidence_id"], art1.evidence_id)


if __name__ == "__main__":
    unittest.main()
