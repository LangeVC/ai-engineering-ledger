"""Schema validation tests for manual importer output."""

import unittest
from decimal import Decimal
import json
import os
import sys
from pathlib import Path

from importers.manual.importer import (
    build_claude_pro_snapshot,
    build_codex_plus_snapshot,
    build_antigravity_snapshot,
    export_canonical_measurements,
)

# Load core semantic check if available in environment
REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_DIR = str(REPO_ROOT / "schema/core")
CATALOG_PATH = os.path.join(SCHEMA_DIR, "units.catalog.json")


class TestManualImporterSchemaValidation(unittest.TestCase):
    """Validates that measurements exported from manual snapshots satisfy core semantic validation."""

    def setUp(self):
        if os.path.exists(CATALOG_PATH):
            with open(CATALOG_PATH, "r", encoding="utf-8") as f:
                self.catalog = json.load(f)
            sys.path.insert(0, str(REPO_ROOT))
            from schema.core.semantic import check_measurement
            self.check_measurement = check_measurement
        else:
            self.catalog = None
            self.check_measurement = None

    def test_claude_measurements_conform_to_units_catalog(self):
        if not self.check_measurement:
            self.skipTest("Core schema catalog not found")
        rec = build_claude_pro_snapshot(
            account_id="claude-pro-main",
            observed_at="2026-10-26T12:00:00Z",
            short_term_remaining_percent=80,
            short_term_reset_at="2026-10-26T15:00:00Z",
        )
        measurements = export_canonical_measurements(rec)
        self.assertEqual(len(measurements), 1)
        errs = self.check_measurement(measurements[0], self.catalog)
        self.assertEqual(errs, [])

    def test_codex_measurements_conform_to_units_catalog(self):
        if not self.check_measurement:
            self.skipTest("Core schema catalog not found")
        rec = build_codex_plus_snapshot(
            account_id="codex-plus-main",
            observed_at="2026-10-26T12:00:00Z",
            short_term_remaining_percent=75,
            short_term_reset_at="2026-10-26T14:30:00Z",
        )
        measurements = export_canonical_measurements(rec)
        self.assertEqual(len(measurements), 1)
        errs = self.check_measurement(measurements[0], self.catalog)
        self.assertEqual(errs, [])

    def test_antigravity_credits_conform_to_units_catalog(self):
        if not self.check_measurement:
            self.skipTest("Core schema catalog not found")
        rec = build_antigravity_snapshot(
            account_id="antigravity-main",
            observed_at="2026-10-26T12:00:00Z",
            credits_consumed="12.5",
            credit_balance="187.5",
        )
        measurements = export_canonical_measurements(rec)
        self.assertEqual(len(measurements), 2)
        for m in measurements:
            errs = self.check_measurement(m, self.catalog)
            self.assertEqual(errs, [])


if __name__ == "__main__":
    unittest.main()
