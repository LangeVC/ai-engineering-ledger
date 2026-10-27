#!/usr/bin/env python3
"""Comprehensive unit and integration tests for importers.files pipeline (PRD US-05.a, US-05.b, US-05.c, US-05.d).
Stdlib unittest only.
"""

from decimal import Decimal
import json
import os
import sys
import unittest

# Ensure repo root is on sys.path
TEST_DIR = os.path.dirname(os.path.abspath(__file__))
# Locate repo root (either parent worktree or main repo with collectors/ and schema/)
curr = TEST_DIR
REPO_ROOT = None
while curr and curr != "/":
    if os.path.isdir(os.path.join(curr, "collectors", "deepseek")) and os.path.isdir(os.path.join(curr, "importers")):
        REPO_ROOT = curr
        break
    curr = os.path.dirname(curr)

if not REPO_ROOT:
    REPO_ROOT = os.path.abspath(os.path.join(TEST_DIR, "../../../../../.."))

# Worktree root is 3 levels up from tests directory: importers/files/tests -> worktree root
WORKTREE_ROOT = os.path.abspath(os.path.join(TEST_DIR, "../../.."))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, WORKTREE_ROOT)

from importers.files import (
    FileImporter,
    FormatOptions,
    QuarantineErrorDetail,
    detect_format_options,
    inspect_tabular_file,
    parse_error_field_and_cause,
)
from importers.files.detector import detect_delimiter, detect_decimal_separator, resolve_timezone_name

# Real minimized DeepSeek export fixture (US-05.b)
REAL_DEEPSEEK_CSV = """date,model,requests,prompt_tokens,prompt_cache_hit_tokens,prompt_cache_miss_tokens,completion_tokens,reasoning_tokens,cost
2026-10-25,deepseek-chat,14,28450,22100,6350,4120,0,0.007621
2026-10-25,deepseek-reasoner,3,15800,8500,7300,3450,2100,0.015240
2026-10-26,deepseek-chat,8,12600,10400,2200,1890,0,0.003348
"""

SAMPLE_JSON = """[
  {"date": "2026-10-25", "model": "deepseek-chat", "requests": 14, "prompt_tokens": 28450, "prompt_cache_hit_tokens": 22100, "prompt_cache_miss_tokens": 6350, "completion_tokens": 4120, "reasoning_tokens": 0, "cost": 0.007621},
  {"date": "2026-10-25", "model": "deepseek-reasoner", "requests": 3, "prompt_tokens": 15800, "prompt_cache_hit_tokens": 8500, "prompt_cache_miss_tokens": 7300, "completion_tokens": 3450, "reasoning_tokens": 2100, "cost": 0.015240}
]"""

SAMPLE_JSONL = """{"date": "2026-10-25", "model": "deepseek-chat", "requests": 14, "prompt_tokens": 28450, "prompt_cache_hit_tokens": 22100, "prompt_cache_miss_tokens": 6350, "completion_tokens": 4120, "reasoning_tokens": 0, "cost": 0.007621}
{"date": "2026-10-26", "model": "deepseek-chat", "requests": 8, "prompt_tokens": 12600, "prompt_cache_hit_tokens": 10400, "prompt_cache_miss_tokens": 2200, "completion_tokens": 1890, "reasoning_tokens": 0, "cost": 0.003348}
"""


class TestFileImporter(unittest.TestCase):

    def setUp(self):
        self.importer = FileImporter()

    def test_us05_a_format_preview_and_detection(self):
        """US-05.a: CSV, JSON und JSONL haben je einen dokumentierten Importweg mit Vorschau;
        CSV unterstützt explizit gewähltes Trennzeichen, Dezimalzeichen und Zeitzone.
        """
        # 1. CSV with semicolon and comma decimal
        csv_semicolon = (
            "date;model;prompt_tokens;cost\n"
            "2026-10-25;deepseek-chat;1000;12,50\n"
            "2026-10-26;deepseek-chat;2000;25,00\n"
        )
        preview_csv = self.importer.preview(
            csv_semicolon,
            format_options=FormatOptions(format="csv", delimiter=";", decimal_separator=",", timezone="Europe/Berlin"),
        )
        self.assertEqual(preview_csv.format, "csv")
        self.assertEqual(preview_csv.detected_options["delimiter"], ";")
        self.assertEqual(preview_csv.detected_options["decimal_separator"], ",")
        self.assertEqual(preview_csv.detected_options["timezone"], "Europe/Berlin")
        self.assertEqual(preview_csv.headers, ["date", "model", "prompt_tokens", "cost"])
        self.assertEqual(len(preview_csv.sample_records), 2)
        self.assertEqual(preview_csv.sample_records[0]["cost"], "12,50")

        # 2. JSON preview
        preview_json = self.importer.preview(SAMPLE_JSON, format_options=FormatOptions(format="json"))
        self.assertEqual(preview_json.format, "json")
        self.assertEqual(len(preview_json.sample_records), 2)
        self.assertIn("prompt_cache_hit_tokens", preview_json.headers)

        # 3. JSONL preview
        preview_jsonl = self.importer.preview(SAMPLE_JSONL, format_options=FormatOptions(format="jsonl"))
        self.assertEqual(preview_jsonl.format, "jsonl")
        self.assertEqual(len(preview_jsonl.sample_records), 2)
        self.assertEqual(preview_jsonl.sample_records[0]["model"], "deepseek-chat")

    def test_us05_a_unsupported_nesting_is_explained_not_guessed(self):
        """US-05.a: Nicht unterstützte Verschachtelung wird erklärt statt geraten."""
        nested_json = json.dumps([
            {
                "date": "2026-10-25",
                "model": "deepseek-chat",
                "usage_details": {
                    "cache_status": "hit",
                    "layers": [1, 2, 3]
                }
            }
        ])
        preview = self.importer.preview(nested_json, format_options=FormatOptions(format="json"))
        self.assertTrue(len(preview.unsupported_nesting_explanations) > 0)
        expl = preview.unsupported_nesting_explanations[0]
        self.assertEqual(expl["field"], "usage_details")
        self.assertIn("unsupported for direct tabular mapping", expl["explanation"])
        self.assertIn("usage_details", expl["explanation"])

    def test_us05_b_real_minimized_deepseek_fixture_and_exact_sums(self):
        """US-05.b: Eine reale minimierte DeepSeek-Fixture wird mit belegter Definition von Input, Cache und Output importiert.
        Die Summen je Account, Zeitraum, Modell und Währung stimmen exakt mit unabhängig berechneten Fixture-Summen überein.
        """
        events, measurements, report = self.importer.import_deepseek_export(
            REAL_DEEPSEEK_CSV,
            format_options=FormatOptions(format="csv", delimiter=",", decimal_separator=".", timezone="UTC"),
            account_alias="deepseek-main",
            currency="usd",
        )

        # Check total record counts
        self.assertEqual(report.total_read, 3)
        self.assertEqual(report.accepted, 3)
        self.assertEqual(report.quarantined, 0)
        self.assertEqual(report.existing, 0)
        self.assertEqual(report.corrected, 0)
        self.assertTrue(report.verify_disjoint_counts())
        self.assertEqual(report.status, "complete")

        # Independent calculation from REAL_DEEPSEEK_CSV rows:
        # Row 1: 2026-10-25, deepseek-chat:
        #   requests: 14, prompt: 28450 (hit: 22100, miss: 6350), comp: 4120, reas: 0, cost: 0.007621
        # Row 2: 2026-10-25, deepseek-reasoner:
        #   requests: 3, prompt: 15800 (hit: 8500, miss: 7300), comp: 3450, reas: 2100, cost: 0.015240
        # Row 3: 2026-10-26, deepseek-chat:
        #   requests: 8, prompt: 12600 (hit: 10400, miss: 2200), comp: 1890, reas: 0, cost: 0.003348

        group1 = report.sums_by_group.get("deepseek-main|2026-10-25|deepseek-chat|usd")
        self.assertIsNotNone(group1)
        self.assertEqual(group1["requests"], 14)
        self.assertEqual(group1["input_tokens"], 28450)
        self.assertEqual(group1["cache_hit_tokens"], 22100)
        self.assertEqual(group1["cache_miss_tokens"], 6350)
        self.assertEqual(group1["output_tokens"], 4120)
        self.assertEqual(group1["reasoning_tokens"], 0)
        self.assertEqual(group1["cost"], Decimal("0.007621"))

        group2 = report.sums_by_group.get("deepseek-main|2026-10-25|deepseek-reasoner|usd")
        self.assertIsNotNone(group2)
        self.assertEqual(group2["requests"], 3)
        self.assertEqual(group2["input_tokens"], 15800)
        self.assertEqual(group2["cache_hit_tokens"], 8500)
        self.assertEqual(group2["cache_miss_tokens"], 7300)
        self.assertEqual(group2["output_tokens"], 3450)
        self.assertEqual(group2["reasoning_tokens"], 2100)
        self.assertEqual(group2["cost"], Decimal("0.015240"))

        group3 = report.sums_by_group.get("deepseek-main|2026-10-26|deepseek-chat|usd")
        self.assertIsNotNone(group3)
        self.assertEqual(group3["requests"], 8)
        self.assertEqual(group3["input_tokens"], 12600)
        self.assertEqual(group3["cache_hit_tokens"], 10400)
        self.assertEqual(group3["cache_miss_tokens"], 2200)
        self.assertEqual(group3["output_tokens"], 1890)
        self.assertEqual(group3["reasoning_tokens"], 0)
        self.assertEqual(group3["cost"], Decimal("0.003348"))

        # Verify generated events and measurements exist and match core structure
        self.assertEqual(len(events), 3)
        self.assertTrue(len(measurements) >= 3 * 5)  # cost, input, output, cache_hit, cache_miss...

    def test_us05_c_disjoint_accounting_and_quarantine(self):
        """US-05.c: Importberichte zählen akzeptierte, bereits vorhandene, quarantänisierte und korrigierte Datensätze.
        Es gilt: eingelesene Datensätze = Summe dieser disjunkten Ergebnisse.
        """
        # CSV with 1 valid row, 1 duplicate row, 1 broken invariant row
        mixed_csv = """date,model,requests,prompt_tokens,prompt_cache_hit_tokens,prompt_cache_miss_tokens,completion_tokens,reasoning_tokens,cost
2026-10-25,deepseek-chat,14,28450,22100,6350,4120,0,0.007621
2026-10-25,deepseek-chat,14,28450,22100,6350,4120,0,0.007621
2026-10-27,deepseek-chat,5,1000,500,100,200,0,0.001000
"""
        importer = FileImporter(strict_mode=False)
        events, measurements, report = importer.import_deepseek_export(mixed_csv)

        # Total read: 3
        # Accepted: 1 (first row)
        # Existing / Duplicate: 1 (second row)
        # Quarantined: 1 (third row, invariant hit+miss=600 != prompt=1000)
        # Corrected: 0
        self.assertEqual(report.total_read, 3)
        self.assertEqual(report.accepted, 1)
        self.assertEqual(report.existing, 1)
        self.assertEqual(report.quarantined, 1)
        self.assertEqual(report.corrected, 0)
        self.assertTrue(report.verify_disjoint_counts())
        self.assertEqual(report.status, "partial")
        self.assertEqual(len(report.errors), 1)
        self.assertIn("Invariant violated", report.errors[0]["error"])

    def test_us05_c_strict_mode_rejects_on_validation_failure(self):
        """Strict mode: If any record fails validation, status is failed and zero records are accepted."""
        mixed_csv = """date,model,requests,prompt_tokens,prompt_cache_hit_tokens,prompt_cache_miss_tokens,completion_tokens,reasoning_tokens,cost
2026-10-25,deepseek-chat,14,28450,22100,6350,4120,0,0.007621
2026-10-27,deepseek-chat,5,1000,500,100,200,0,0.001000
"""
        importer = FileImporter(strict_mode=True)
        events, measurements, report = importer.import_deepseek_export(mixed_csv)

        self.assertEqual(report.total_read, 2)
        self.assertEqual(report.status, "failed")
        self.assertEqual(report.accepted, 0)
        self.assertEqual(report.quarantined, 1)
        self.assertEqual(len(events), 0)
        self.assertEqual(len(measurements), 0)

    def test_negative_probe_faulty_invariants(self):
        """Negative probe: reasoning > completion tokens or negative tokens must be quarantined."""
        negative_csv = """date,model,requests,prompt_tokens,prompt_cache_hit_tokens,prompt_cache_miss_tokens,completion_tokens,reasoning_tokens,cost
2026-10-25,deepseek-reasoner,1,1000,500,500,100,200,0.001
2026-10-25,deepseek-chat,1,1000,-100,1100,200,0,0.001
"""
        importer = FileImporter(strict_mode=False)
        events, measurements, report = importer.import_deepseek_export(negative_csv)

        self.assertEqual(report.total_read, 2)
        self.assertEqual(report.accepted, 0)
        self.assertEqual(report.quarantined, 2)
        self.assertTrue(report.verify_disjoint_counts())
        self.assertEqual(report.status, "partial")

    def test_us05_d_error_isolation_and_quarantine_reporting(self):
        """US-05.d: Fehlerhafte Datensätze werden isoliert; bei zulässigem Teilimport ist der Lauf 'partial',
        niemals 'complete'. Ausführliche Fehlerberichte mit Zeilenbezug, Feld und Ursache.
        Gültige Zeilen im selben Lauf bleiben erhalten.
        """
        # CSV with 1 valid row (line 1), 1 missing date (line 2), 1 invalid decimal cost (line 3),
        # 1 invariant violation reasoning > completion (line 4), and 1 valid row (line 5)
        mixed_csv = (
            "date,model,requests,prompt_tokens,prompt_cache_hit_tokens,prompt_cache_miss_tokens,completion_tokens,reasoning_tokens,cost\n"
            "2026-10-25,deepseek-chat,10,1000,800,200,200,50,0.002000\n"
            ",deepseek-chat,5,500,400,100,100,0,0.001000\n"
            "2026-10-26,deepseek-chat,5,500,400,100,100,0,invalid_cost\n"
            "2026-10-27,deepseek-reasoner,2,1000,500,500,100,250,0.005000\n"
            "2026-10-28,deepseek-chat,8,800,600,200,150,0,0.001500\n"
        )
        importer = FileImporter(strict_mode=False)
        events, measurements, report = importer.import_deepseek_export(mixed_csv)

        # 5 total rows read
        self.assertEqual(report.total_read, 5)
        # Valid rows retained: 2 (line 1 and line 5)
        self.assertEqual(report.accepted, 2)
        # Quarantined rows isolated: 3 (lines 2, 3, 4)
        self.assertEqual(report.quarantined, 3)
        self.assertEqual(report.existing, 0)
        self.assertEqual(report.corrected, 0)
        self.assertTrue(report.verify_disjoint_counts())
        # Run must be partial, NEVER complete
        self.assertEqual(report.status, "partial")
        self.assertNotEqual(report.status, "complete")

        # Events and measurements preserved for valid rows
        self.assertEqual(len(events), 2)
        self.assertTrue(len(measurements) > 0)

        # Verify detailed quarantine reporting: line reference (1-based physical CSV line: line 1 is header), field, cause
        self.assertEqual(len(report.quarantine_details), 3)

        # Quarantined record 1: CSV line 3 (second data row) - missing date
        q1 = report.quarantine_details[0]
        self.assertEqual(q1.line_number, 3)
        self.assertEqual(q1.field, "date")
        self.assertIn("Missing required field", q1.cause)

        # Quarantined record 2: CSV line 4 (third data row) - invalid cost decimal
        q2 = report.quarantine_details[1]
        self.assertEqual(q2.line_number, 4)
        self.assertEqual(q2.field, "cost")
        self.assertIn("Invalid decimal", q2.cause)

        # Quarantined record 3: CSV line 5 (fourth data row) - reasoning > completion
        q3 = report.quarantine_details[2]
        self.assertEqual(q3.line_number, 5)
        self.assertEqual(q3.field, "reasoning_tokens")
        self.assertIn("Invariant violated", q3.cause)

        # Verify error dict list matches quarantine details
        for err in report.errors:
            self.assertIn("line", err)
            self.assertIn("field", err)
            self.assertIn("cause", err)
            self.assertIsNotNone(err["line"])
            self.assertIsNotNone(err["field"])

    def test_us05_d_generic_export_error_isolation_and_reporting(self):
        """US-05.d: Generic mapping export isolates invalid rows, preserves valid rows,
        and provides detailed quarantine error reports with row_number, field, and cause.
        """
        from importers.mapping.models import ColumnMappingRule, CsvOptions

        mapping_cfg = {
            "source": "custom_provider",
            "account_alias": "cust-01",
            "format": "csv",
            "column_mapping": {
                "period": {"target_field": "period_start", "target_type": "iso8601_date", "required": True},
                "model_name": {"target_field": "model", "target_type": "string", "required": True},
                "tokens": {"target_metric": "tokens", "target_type": "integer", "unit": "count", "required": True},
            },
            "csv_options": {"delimiter": ","},
        }

        # 3 rows: row 1 valid, row 2 invalid integer tokens, row 3 valid
        csv_content = (
            "period,model_name,tokens\n"
            "2026-10-25,model-alpha,1500\n"
            "2026-10-26,model-alpha,not_a_number\n"
            "2026-10-27,model-beta,3000\n"
        )

        importer = FileImporter(strict_mode=False)
        events, measurements, report = importer.import_generic_export(
            csv_content,
            mapping_config_dict=mapping_cfg,
        )

        self.assertEqual(report.total_read, 3)
        self.assertEqual(report.accepted, 2)
        self.assertEqual(report.quarantined, 1)
        self.assertTrue(report.verify_disjoint_counts())
        self.assertEqual(report.status, "partial")
        self.assertNotEqual(report.status, "complete")

        # Events for valid rows
        self.assertEqual(len(events), 2)

        # Detailed quarantine report
        self.assertEqual(len(report.quarantine_details), 1)
        qd = report.quarantine_details[0]
        self.assertEqual(qd.line_number, 2)
        self.assertEqual(qd.field, "tokens")
        self.assertIn("invalid integer", qd.cause)


if __name__ == "__main__":
    unittest.main()
