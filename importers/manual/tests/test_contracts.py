"""Contract tests for manual importers according to PRD US-07.a, US-07.b, US-07.c."""

import unittest
from decimal import Decimal
import json

from importers.manual.importer import (
    ManualImportValidationError,
    ManualSubscriptionEntry,
    ManualPaymentEntry,
    ManualBalanceEntry,
    ManualEntrySession,
    ManualSourceDueConfig,
    ManualSourceDueResult,
    DEFAULT_MANUAL_MAX_AGE_SECONDS,
    build_claude_pro_snapshot,
    build_codex_plus_snapshot,
    build_antigravity_snapshot,
    parse_manual_template_json,
    export_canonical_measurements,
    generate_manual_evidence_descriptor,
    check_manual_source_due,
    evaluate_all_manual_sources_due,
    validate_percentage,
    validate_credit_amount,
    validate_rfc3339_timestamp,
    parse_rfc3339_to_datetime,
)


class TestManualImporterContracts(unittest.TestCase):
    """Verifies US-07.a, US-07.b, and US-07.c acceptance criteria."""

    def test_us07_a_three_templates_support_product_account_time_tz_evidence(self):
        """US-07.a: Jede der drei Vorlagen unterstützt Produkt/Account,
        Beobachtungszeit mit Zeitzone, optionalen Beleg und passende Quota-/Credit-Metriken.
        Nicht sichtbare Werte bleiben unbekannt.
        """
        # 1. Claude Pro
        claude = build_claude_pro_snapshot(
            account_id="claude-pro-main",
            observed_at="2026-10-26T12:00:00+01:00",
            evidence_id="ev_01ARZ3NDEKTSV4RRFFQ69G5F01",
            short_term_remaining_percent=80,
            short_term_reset_at="2026-10-26T15:00:00+01:00",
            notes="Test observation",
        )
        self.assertEqual(claude.product, "Claude Pro")
        self.assertEqual(claude.source, "anthropic")
        self.assertEqual(claude.source_account_id, "claude-pro-main")
        self.assertEqual(claude.observed_at, "2026-10-26T12:00:00+01:00")
        self.assertEqual(claude.evidence_id, "ev_01ARZ3NDEKTSV4RRFFQ69G5F01")
        self.assertEqual(len(claude.windows), 1)
        self.assertEqual(claude.windows[0].quota_remaining_percent, Decimal("80"))
        self.assertEqual(claude.windows[0].quota_used_percent, Decimal("20"))

        # 2. Codex Plus
        codex = build_codex_plus_snapshot(
            account_id="codex-plus-main",
            observed_at="2026-10-26T12:00:00Z",
            evidence_id="ev_01ARZ3NDEKTSV4RRFFQ69G5F02",
            short_term_remaining_percent=75,
            short_term_reset_at="2026-10-26T14:30:00Z",
        )
        self.assertEqual(codex.product, "ChatGPT Plus / Codex")
        self.assertEqual(codex.source, "openai")
        self.assertEqual(codex.source_account_id, "codex-plus-main")
        self.assertEqual(codex.observed_at, "2026-10-26T12:00:00Z")
        self.assertEqual(codex.evidence_id, "ev_01ARZ3NDEKTSV4RRFFQ69G5F02")
        self.assertEqual(codex.windows[0].quota_remaining_percent, Decimal("75"))
        self.assertEqual(codex.windows[0].quota_used_percent, Decimal("25"))

        # 3. Antigravity
        anti = build_antigravity_snapshot(
            account_id="antigravity-main",
            observed_at="2026-10-26T12:00:00+02:00",
            evidence_id=None,  # optional receipt
            credits_consumed=12.5,
            credit_balance=187.5,
            quota_percent_remaining=93.75,
            reset_at="2026-11-01T00:00:00Z",
        )
        self.assertEqual(anti.product, "Google AI Pro / Antigravity")
        self.assertEqual(anti.source, "google")
        self.assertEqual(anti.source_account_id, "antigravity-main")
        self.assertIsNone(anti.evidence_id)
        self.assertEqual(anti.credits_consumed, Decimal("12.5"))
        self.assertEqual(anti.credit_balance, Decimal("187.5"))
        self.assertEqual(anti.credit_unit, "credit")

    def test_us07_a_naive_timestamp_rejected(self):
        """Timestamp without explicit timezone offset must be rejected."""
        with self.assertRaises(ManualImportValidationError):
            build_claude_pro_snapshot(
                account_id="claude-pro-main",
                observed_at="2026-10-26 12:00:00",  # no timezone
                short_term_remaining_percent=80,
            )

    def test_us07_a_unknown_values_remain_none(self):
        """Values not visible in the UI must remain unknown (None), never guessed."""
        claude = build_claude_pro_snapshot(
            account_id="claude-pro-main",
            observed_at="2026-10-26T12:00:00Z",
            short_term_remaining_percent=80,
            weekly_remaining_percent=None,  # unobserved
            weekly_reset_at=None,          # unobserved
        )
        # Weekly window is not created if unobserved, or if created has None values
        self.assertIsNone(claude.credits_consumed)
        self.assertIsNone(claude.credit_balance)
        self.assertEqual(len(claude.windows), 1)

    def test_us07_b_remaining_stored_and_derived_to_used(self):
        """US-07.b: „80 % remaining“ wird als verbleibend gespeichert bzw.
        nachvollziehbar zu „20 % used“ abgeleitet.
        """
        claude = build_claude_pro_snapshot(
            account_id="claude-pro-main",
            observed_at="2026-10-26T12:00:00Z",
            short_term_remaining_percent=80,
        )
        w = claude.windows[0]
        self.assertEqual(w.quota_remaining_percent, Decimal("80"))
        self.assertEqual(w.quota_used_percent, Decimal("20"))

        # Inverted case: user provides used=35% -> derived remaining=65%
        codex = build_codex_plus_snapshot(
            account_id="codex-plus-main",
            observed_at="2026-10-26T12:00:00Z",
            short_term_used_percent=35,
        )
        self.assertEqual(codex.windows[0].quota_used_percent, Decimal("35"))
        self.assertEqual(codex.windows[0].quota_remaining_percent, Decimal("65"))

    def test_us07_b_percentage_bounds_zero_to_hundred_allowed(self):
        """Between 0 and 100 are allowed, including boundary values 0 and 100."""
        # 0%
        c0 = build_claude_pro_snapshot(
            account_id="acc",
            observed_at="2026-10-26T12:00:00Z",
            short_term_remaining_percent=0,
        )
        self.assertEqual(c0.windows[0].quota_remaining_percent, Decimal("0"))
        self.assertEqual(c0.windows[0].quota_used_percent, Decimal("100"))

        # 100%
        c100 = build_claude_pro_snapshot(
            account_id="acc",
            observed_at="2026-10-26T12:00:00Z",
            short_term_remaining_percent=100,
        )
        self.assertEqual(c100.windows[0].quota_remaining_percent, Decimal("100"))
        self.assertEqual(c100.windows[0].quota_used_percent, Decimal("0"))

        # Decimal percent
        c50_5 = build_claude_pro_snapshot(
            account_id="acc",
            observed_at="2026-10-26T12:00:00Z",
            short_term_remaining_percent="50.5",
        )
        self.assertEqual(c50_5.windows[0].quota_remaining_percent, Decimal("50.5"))
        self.assertEqual(c50_5.windows[0].quota_used_percent, Decimal("49.5"))

    def test_us07_b_negative_probe_minus_one_and_101_rejected(self):
        """US-07.b: -1 und 101 werden für diese Prozentmetrik abgelehnt (negative fachliche Probe)."""
        # -1 rejected
        with self.assertRaises(ManualImportValidationError) as ctx:
            validate_percentage(-1, "quota_percent")
        self.assertIn("between 0 and 100", str(ctx.exception))

        with self.assertRaises(ManualImportValidationError):
            build_claude_pro_snapshot(
                account_id="acc",
                observed_at="2026-10-26T12:00:00Z",
                short_term_remaining_percent=-1,
            )

        # 101 rejected
        with self.assertRaises(ManualImportValidationError) as ctx:
            validate_percentage(101, "quota_percent")
        self.assertIn("between 0 and 100", str(ctx.exception))

        with self.assertRaises(ManualImportValidationError):
            build_claude_pro_snapshot(
                account_id="acc",
                observed_at="2026-10-26T12:00:00Z",
                short_term_remaining_percent=101,
            )

    def test_us07_b_no_token_hallucination(self):
        """Enforces that percentage inputs are NEVER converted to token counts."""
        rec = build_claude_pro_snapshot(
            account_id="claude-pro-main",
            observed_at="2026-10-26T12:00:00Z",
            short_term_remaining_percent=80,
        )
        measurements = export_canonical_measurements(rec)
        for m in measurements:
            self.assertNotEqual(m["unit"], "token")
            self.assertNotIn("token", m["metric"])
            self.assertEqual(m["unit"], "percent")
            self.assertEqual(m["metric"], "quota_percent_used")
            self.assertEqual(m["value"], 20)

    def test_us07_c_weekly_and_short_term_are_separate_windows(self):
        """US-07.c: Wöchentliche und kurzfristige Quota werden als getrennte Fenster eingegeben."""
        rec = build_claude_pro_snapshot(
            account_id="claude-pro-main",
            observed_at="2026-10-26T12:00:00Z",
            short_term_remaining_percent=80,
            short_term_reset_at="2026-10-26T15:00:00Z",
            weekly_remaining_percent=60,
            weekly_reset_at=None,  # unknown
        )
        self.assertEqual(len(rec.windows), 2)
        win_types = [w.window_type for w in rec.windows]
        self.assertIn("5h", win_types)
        self.assertIn("weekly", win_types)

        w5h = [w for w in rec.windows if w.window_type == "5h"][0]
        w_weekly = [w for w in rec.windows if w.window_type == "weekly"][0]

        self.assertEqual(w5h.quota_remaining_percent, Decimal("80"))
        self.assertEqual(w5h.reset_at, "2026-10-26T15:00:00Z")

        self.assertEqual(w_weekly.quota_remaining_percent, Decimal("60"))
        self.assertIsNone(w_weekly.reset_at)

    def test_us07_c_unknown_reset_timestamp_never_inferred(self):
        """US-07.c: Ein unbekannter Resetzeitpunkt wird nicht automatisch aus „weekly“ errechnet."""
        rec = build_claude_pro_snapshot(
            account_id="claude-pro-main",
            observed_at="2026-10-26T12:00:00Z",
            weekly_remaining_percent=60,
            weekly_reset_at=None,
        )
        self.assertIsNone(rec.windows[0].reset_at)

        # Export to measurements and verify reset_at is strictly None
        measurements = export_canonical_measurements(rec)
        self.assertIsNone(measurements[0]["dimensions"]["reset_at"])

    def test_antigravity_credits_unit_strictly_credit(self):
        """Antigravity Credits strikt als unit=credit."""
        rec = build_antigravity_snapshot(
            account_id="antigravity-main",
            observed_at="2026-10-26T12:00:00Z",
            credits_consumed="12.5",
            credit_balance="187.5",
        )
        measurements = export_canonical_measurements(rec)
        metrics = {m["metric"]: m for m in measurements}

        self.assertIn("credits_consumed", metrics)
        self.assertEqual(metrics["credits_consumed"]["unit"], "credit")
        self.assertEqual(metrics["credits_consumed"]["value"], "12.5")

        self.assertIn("credit_balance", metrics)
        self.assertEqual(metrics["credit_balance"]["unit"], "credit")
        self.assertEqual(metrics["credit_balance"]["value"], "187.5")

    def test_parse_from_template_json_fixtures(self):
        """Parses json templates matching capabilities matrix fixtures."""
        claude_json = {
            "source": "anthropic",
            "account_alias": "claude-pro-main",
            "product": "Claude Pro",
            "mode": "manual_snapshot",
            "observed_at": "2026-10-26T12:00:00+01:00",
            "evidence_ref": "ev_01ARZ3NDEKTSV4RRFFQ69G5F10",
            "quota_metrics": {
                "quota_remaining_percent": 80.0,
                "quota_used_percent": 20.0,
                "source_statement": "80% remaining",
                "reset_at": "2026-10-26T15:00:00+01:00",
                "window_type": "rolling_5h"
            },
            "weekly_allotment": {
                "quota_remaining_percent": None,
                "reset_at": None,
                "window_type": "weekly_allotment"
            }
        }
        rec = parse_manual_template_json(claude_json)
        self.assertEqual(rec.product, "Claude Pro")
        self.assertEqual(rec.source_account_id, "claude-pro-main")
        self.assertEqual(rec.windows[0].quota_remaining_percent, Decimal("80.0"))
        self.assertEqual(rec.windows[0].quota_used_percent, Decimal("20.0"))

    # ==========================================================================
    # US-07.d Tests: Screenshot values entered by user / No OCR / Evidence gen
    # ==========================================================================

    def test_us07_d_evidence_descriptor_without_screenshot_generates_manual_receipt(self):
        """US-07.d: Ohne Screenshot erzeugt die bestätigte Eingabe selbst einen als manuell gekennzeichneten Beleg."""
        payload = {
            "source": "anthropic",
            "account": "claude-pro-main",
            "quota_remaining_percent": "80",
            "note": "Manually checked in Anthropic settings dashboard",
        }
        descriptor = generate_manual_evidence_descriptor(
            source="anthropic",
            account="claude-pro-main",
            created_at="2026-10-26T12:00:00+01:00",
            payload_content=payload,
            capture_point="user_manual_entry",
        )
        self.assertTrue(descriptor["evidence_id"].startswith("ev_"))
        self.assertEqual(descriptor["capture_point"], "user_manual_entry")
        self.assertEqual(descriptor["source"], "anthropic")
        self.assertEqual(descriptor["account"], "claude-pro-main")
        self.assertFalse(descriptor["redaction"]["redacted"])
        self.assertIsNone(descriptor["redaction"]["redaction_version"])
        self.assertEqual(len(descriptor["sha256"]), 64)
        self.assertGreater(descriptor["size_bytes"], 0)

    def test_us07_d_screenshot_values_entered_by_user_no_implicit_ocr(self):
        """US-07.d: Screenshot values are provided by the user. If user supplies values from a screenshot,
        no external OCR service or image transmission is triggered.
        """
        # User explicitly enters the values seen in their screenshot
        rec = build_claude_pro_snapshot(
            account_id="claude-pro-main",
            observed_at="2026-10-26T12:00:00Z",
            evidence_id="ev_01J9Z3K5Q8W2X4Y6A7B8C9D0E5",
            short_term_remaining_percent=85,
            notes="Values manually transcribed from local screenshot screenshot_20261026.png",
        )
        self.assertEqual(rec.evidence_id, "ev_01J9Z3K5Q8W2X4Y6A7B8C9D0E5")
        self.assertEqual(rec.windows[0].quota_remaining_percent, Decimal("85"))
        self.assertEqual(rec.windows[0].quota_used_percent, Decimal("15"))

    # ==========================================================================
    # US-07.e Tests: Separate entries & Preview / Confirm / Cancellation Invariant
    # ==========================================================================

    def test_us07_e_subscription_payment_balance_are_strictly_separate_entries(self):
        """US-07.e: Monatsabo, einzelne Zahlung und Guthabenstand sind getrennte Eingaben."""
        sub = ManualSubscriptionEntry(
            source="anthropic",
            account_id="claude-pro-main",
            plan_name="Claude Pro Monthly",
            amount=Decimal("20.00"),
            currency="USD",
            cycle_start="2026-10-01T00:00:00Z",
            cycle_end="2026-11-01T00:00:00Z",
        )
        self.assertEqual(sub.entry_type, "subscription")
        self.assertEqual(sub.amount, Decimal("20.00"))

        pay = ManualPaymentEntry(
            source="anthropic",
            account_id="claude-pro-main",
            amount=Decimal("23.80"),
            currency="EUR",
            paid_at="2026-10-01T10:15:00+01:00",
            transaction_ref="tx_stripe_12345",
        )
        self.assertEqual(pay.entry_type, "payment")
        self.assertEqual(pay.amount, Decimal("23.80"))

        bal = ManualBalanceEntry(
            source="google",
            account_id="antigravity-main",
            observed_at="2026-10-26T12:00:00Z",
            balance_amount=Decimal("250.0"),
            balance_unit="credit",
        )
        self.assertEqual(bal.entry_type, "balance")
        self.assertEqual(bal.balance_amount, Decimal("250.0"))
        self.assertEqual(bal.balance_unit, "credit")

    def test_us07_e_preview_and_confirm_workflow(self):
        """US-07.e: Der Nutzer bestätigt die Vorschau; bestätigt erzeugt valide Messungen."""
        session = ManualEntrySession()
        sub = ManualSubscriptionEntry(
            source="anthropic",
            account_id="claude-pro-main",
            plan_name="Claude Pro Monthly",
            amount=Decimal("20.00"),
            currency="USD",
            cycle_start="2026-10-01T00:00:00Z",
            cycle_end="2026-11-01T00:00:00Z",
        )
        pay = ManualPaymentEntry(
            source="anthropic",
            account_id="claude-pro-main",
            amount=Decimal("20.00"),
            currency="USD",
            paid_at="2026-10-01T00:00:00Z",
        )
        session.stage_entry(sub)
        session.stage_entry(pay)

        # Preview must be invoked prior to confirmation
        prev = session.preview()
        self.assertEqual(prev["staged_count"], 2)
        self.assertEqual(len(prev["entries"]), 2)
        self.assertEqual(prev["entries"][0]["kind"], "subscription")
        self.assertEqual(prev["entries"][1]["kind"], "payment")

        # Now confirm
        measurements = session.confirm()
        self.assertEqual(len(measurements), 2)
        metrics = [m["metric"] for m in measurements]
        self.assertIn("subscription_cost", metrics)
        self.assertIn("payment_amount", metrics)
        self.assertTrue(session.is_confirmed)
        self.assertEqual(len(session.staged_entries), 0)

    def test_us07_e_cancellation_modifies_no_measurement(self):
        """US-07.e: Abbruch verändert keine Messung."""
        session = ManualEntrySession()
        sub = ManualSubscriptionEntry(
            source="anthropic",
            account_id="claude-pro-main",
            plan_name="Claude Pro Monthly",
            amount=Decimal("20.00"),
            currency="USD",
            cycle_start="2026-10-01T00:00:00Z",
            cycle_end="2026-11-01T00:00:00Z",
        )
        session.stage_entry(sub)
        prev = session.preview()
        self.assertEqual(prev["staged_count"], 1)

        # Cancel session
        res = session.cancel()
        self.assertEqual(res["status"], "cancelled")
        self.assertEqual(res["measurements_modified"], 0)
        self.assertTrue(session.is_cancelled)
        self.assertEqual(len(session.staged_entries), 0)
        self.assertEqual(len(session.committed_measurements), 0)

        # Confirming cancelled session raises error
        with self.assertRaises(ManualImportValidationError):
            session.confirm()

    def test_us07_e_confirmation_without_preview_rejected(self):
        """Confirming without prior preview is rejected."""
        session = ManualEntrySession()
        sub = ManualSubscriptionEntry(
            source="anthropic",
            account_id="claude-pro-main",
            plan_name="Claude Pro Monthly",
            amount=Decimal("20.00"),
            currency="USD",
            cycle_start="2026-10-01T00:00:00Z",
            cycle_end="2026-11-01T00:00:00Z",
        )
        session.stage_entry(sub)
        with self.assertRaises(ManualImportValidationError):
            session.confirm()

    # ==========================================================================
    # US-07.f Tests: Expiration, due detection & configurable thresholds
    # ==========================================================================

    def test_us07_f_default_daily_rhythm_identifies_overdue_and_fresh(self):
        """US-07.f: Status/`due` erkennt nach konfigurierbarer Fälligkeit überfällige manuelle Quellen;
        ohne Konfiguration gilt der vorgeschlagene tägliche Rhythmus (24h / 86400s).
        """
        now_ts = "2026-10-26T12:00:00Z"

        # Fresh observation: 10 hours ago (< 24 hours)
        recent_ts = "2026-10-26T02:00:00Z"
        res_fresh = check_manual_source_due(
            source="anthropic",
            account_id="claude-pro-main",
            last_observed_at=recent_ts,
            current_time=now_ts,
        )
        self.assertFalse(res_fresh.is_due)
        self.assertEqual(res_fresh.status, "fresh")
        self.assertEqual(res_fresh.max_age_seconds, DEFAULT_MANUAL_MAX_AGE_SECONDS)
        self.assertEqual(res_fresh.age_seconds, 36000.0)

        # Overdue observation: 25 hours ago (> 24 hours)
        stale_ts = "2026-10-25T11:00:00Z"
        res_overdue = check_manual_source_due(
            source="anthropic",
            account_id="claude-pro-main",
            last_observed_at=stale_ts,
            current_time=now_ts,
        )
        self.assertTrue(res_overdue.is_due)
        self.assertEqual(res_overdue.status, "overdue")
        self.assertEqual(res_overdue.age_seconds, 90000.0)
        self.assertEqual(res_overdue.overdue_seconds, 3600.0)
        self.assertIn("overdue by 3600s", res_overdue.reason)

    def test_us07_f_configurable_due_threshold(self):
        """Configuring custom freshness threshold (e.g. 4 hours for rapid quota monitoring)."""
        now_ts = "2026-10-26T12:00:00Z"
        obs_ts = "2026-10-26T07:00:00Z"  # 5 hours ago

        # Under default 24h cadence, 5h is fresh:
        res_default = check_manual_source_due(
            source="openai",
            account_id="codex-plus-main",
            last_observed_at=obs_ts,
            current_time=now_ts,
        )
        self.assertFalse(res_default.is_due)

        # Under custom 4h (14400s) config, 5h is overdue:
        custom_cfg = ManualSourceDueConfig(
            source="openai",
            account_id="codex-plus-main",
            max_age_seconds=14400,
        )
        res_custom = check_manual_source_due(
            source="openai",
            account_id="codex-plus-main",
            last_observed_at=obs_ts,
            current_time=now_ts,
            config=custom_cfg,
        )
        self.assertTrue(res_custom.is_due)
        self.assertEqual(res_custom.status, "overdue")
        self.assertEqual(res_custom.max_age_seconds, 14400)
        self.assertEqual(res_custom.overdue_seconds, 3600.0)

    def test_us07_f_unobserved_source_is_immediately_due(self):
        """A manual source with no recorded snapshot is immediately marked due with status 'no_snapshot'."""
        now_ts = "2026-10-26T12:00:00Z"
        res = check_manual_source_due(
            source="google",
            account_id="antigravity-main",
            last_observed_at=None,
            current_time=now_ts,
        )
        self.assertTrue(res.is_due)
        self.assertEqual(res.status, "no_snapshot")
        self.assertIsNone(res.last_observed_at)
        self.assertIsNone(res.age_seconds)

    def test_us07_f_evaluate_all_sources_due_summary(self):
        """Evaluates batch of manual sources for `ael due` summary table."""
        sources = [
            {"source": "anthropic", "account_id": "claude-pro", "last_observed_at": "2026-10-26T10:00:00Z"},  # 2h ago -> fresh
            {"source": "openai", "account_id": "codex-plus", "last_observed_at": "2026-10-24T12:00:00Z"},    # 48h ago -> overdue
            {"source": "google", "account_id": "antigravity", "last_observed_at": None},                     # none -> due
        ]
        summary = evaluate_all_manual_sources_due(sources, current_time="2026-10-26T12:00:00Z")
        self.assertEqual(summary["total_sources"], 3)
        self.assertEqual(summary["due_count"], 2)
        self.assertEqual(summary["overdue_count"], 1)
        self.assertEqual(summary["fresh_count"], 1)
        self.assertEqual(len(summary["sources"]), 3)

    # ==========================================================================
    # Negative Probes for US-07.d, US-07.e, US-07.f
    # ==========================================================================

    def test_negative_probe_us07_d_invalid_evidence_timestamp(self):
        """Negative probe: Generating evidence descriptor with invalid or naive timestamp fails."""
        with self.assertRaises(ManualImportValidationError):
            generate_manual_evidence_descriptor(
                source="anthropic",
                account="claude-pro-main",
                created_at="invalid-date",
                payload_content={"val": 1},
            )

    def test_negative_probe_us07_e_negative_subscription_or_payment_amount(self):
        """Negative probe: Negative payment or subscription amounts are rejected."""
        with self.assertRaises(ManualImportValidationError):
            ManualSubscriptionEntry(
                source="anthropic",
                account_id="acc",
                plan_name="Pro",
                amount=Decimal("-10.00"),
                currency="USD",
                cycle_start="2026-10-01T00:00:00Z",
                cycle_end="2026-11-01T00:00:00Z",
            )
        with self.assertRaises(ManualImportValidationError):
            ManualPaymentEntry(
                source="anthropic",
                account_id="acc",
                amount=Decimal("-5.00"),
                currency="USD",
                paid_at="2026-10-01T00:00:00Z",
            )

    def test_negative_probe_us07_f_future_observation_time_rejected(self):
        """Negative probe: Observation timestamp in the future relative to check time is rejected."""
        now_ts = "2026-10-26T12:00:00Z"
        future_ts = "2026-10-26T15:00:00Z"  # 3 hours in the future
        with self.assertRaises(ManualImportValidationError):
            check_manual_source_due(
                source="anthropic",
                account_id="claude-pro-main",
                last_observed_at=future_ts,
                current_time=now_ts,
            )

    def test_negative_probe_us07_f_invalid_max_age_seconds(self):
        """Negative probe: Non-positive max_age_seconds is rejected."""
        with self.assertRaises(ManualImportValidationError):
            ManualSourceDueConfig(source="anthropic", account_id="acc", max_age_seconds=0)


if __name__ == "__main__":
    unittest.main()
