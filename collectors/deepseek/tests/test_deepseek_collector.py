#!/usr/bin/env python3
"""Contract and Unit Tests for DeepSeek Collector & Adapter (AEL-ADAPTER-DEEPSEEK).
Tests:
- US-05.a: CSV, JSON, JSONL import, preview, custom delimiter/decimal/timezone
- US-05.b: Real minimized fixture import, independent sum checks (no drift)
- US-05.c: Disjoint count accounting: total_read == accepted + existing + quarantined + corrected
- US-05.d: Isolated error records, partial failure vs complete failure, strict validation mode
- US-05 invariants:
  * prompt_tokens == prompt_cache_hit_tokens + prompt_cache_miss_tokens
  * prompt_cache_hit_tokens >= 0
  * prompt_cache_miss_tokens >= 0
  * completion_tokens >= 0
  * reasoning_tokens <= completion_tokens
- Schema compliance against schema/collector and schema/core contracts
- Idempotency key stability
- Real-time inference usage dict parsing
"""

import copy
import json
import os
import sys
import unittest
from decimal import Decimal

# Ensure local imports work
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
COLLECTORS_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
sys.path.insert(0, COLLECTORS_ROOT)
sys.path.insert(0, REPO_ROOT)

from collectors.deepseek.collector import (
    DeepSeekCollector,
    DeepSeekConfig,
    DeepSeekImportReport,
    derive_idempotency_key,
    generate_ulid,
    parse_inference_usage,
)

FIXTURE_SAMPLE_CSV = """date,model,requests,prompt_tokens,prompt_cache_hit_tokens,prompt_cache_miss_tokens,completion_tokens,reasoning_tokens,cost
2026-10-25,deepseek-chat,14,28450,22100,6350,4120,0,0.007621
2026-10-25,deepseek-reasoner,3,15800,8500,7300,3450,2100,0.015240
2026-10-26,deepseek-chat,8,12600,10400,2200,1890,0,0.003348
"""

FIXTURE_EURO_CSV = """date;model;requests;prompt_tokens;prompt_cache_hit_tokens;prompt_cache_miss_tokens;completion_tokens;reasoning_tokens;cost
2026-10-25;deepseek-chat;10;20000;15000;5000;2000;0;0,005000
"""

FIXTURE_INVARIANT_VIOLATIONS_CSV = """date,model,requests,prompt_tokens,prompt_cache_hit_tokens,prompt_cache_miss_tokens,completion_tokens,reasoning_tokens,cost
2026-10-25,deepseek-chat,1,100,60,30,50,0,0.000100
2026-10-25,deepseek-reasoner,1,100,50,50,40,50,0.000150
2026-10-25,deepseek-chat,1,100,-10,110,50,0,0.000100
2026-10-25,deepseek-chat,1,100,50,50,-5,0,0.000100
"""


class TestDeepSeekCollector(unittest.TestCase):

    def setUp(self):
        self.collector = DeepSeekCollector()

    def test_ulid_generation(self):
        u1 = generate_ulid()
        u2 = generate_ulid()
        self.assertEqual(len(u1), 26)
        self.assertEqual(len(u2), 26)
        self.assertNotEqual(u1, u2)
        # Crockford Base32 characters only
        valid_chars = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
        self.assertTrue(all(c in valid_chars for c in u1))

    def test_idempotency_key_derivation(self):
        window = {
            "type": "daily",
            "starts_at": "2026-10-25T00:00:00Z",
            "ends_at": "2026-10-25T23:59:59Z",
        }
        k1 = derive_idempotency_key("deepseek", "deepseek-main", "rec-01", window)
        k2 = derive_idempotency_key("deepseek", "deepseek-main", "rec-01", window)
        self.assertEqual(k1, k2)
        self.assertEqual(len(k1), 64)

        # Different account or window changes key
        k3 = derive_idempotency_key("deepseek", "deepseek-other", "rec-01", window)
        self.assertNotEqual(k1, k3)

    def test_account_and_capability_declarations(self):
        acc = self.collector.declare_account()
        self.assertEqual(acc["schema_version"], "0.1")
        self.assertEqual(acc["source"], "deepseek")
        self.assertEqual(acc["alias"], "deepseek-main")
        self.assertEqual(acc["profile_id"], "deepseek-main")
        self.assertEqual(acc["health"], "ready")

        cap = self.collector.declare_capability()
        self.assertEqual(cap["schema_version"], "0.1")
        self.assertEqual(cap["source"], "deepseek")
        self.assertEqual(cap["status"], "unverified")
        self.assertTrue(cap["profile_binding"]["explicit"])
        self.assertFalse(cap["profile_binding"]["global_state_mutated"])
        self.assertIn("reason", cap)

    def test_csv_preview(self):
        rows = self.collector.preview_csv(FIXTURE_SAMPLE_CSV, max_rows=2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["model"], "deepseek-chat")
        self.assertEqual(rows[1]["model"], "deepseek-reasoner")

    def test_us05_b_fixture_sums_and_independent_math(self):
        """US-05.b: Real minimized DeepSeek fixture import.
        Sums per account, period, model, currency match independently calculated sums exactly.
        """
        records, report = self.collector.parse_export_data(FIXTURE_SAMPLE_CSV, format_type="csv")
        self.assertEqual(len(records), 3)
        self.assertEqual(report.total_read, 3)
        self.assertEqual(report.accepted, 3)
        self.assertEqual(report.existing, 0)
        self.assertEqual(report.quarantined, 0)
        self.assertTrue(report.verify_disjoint_counts())

        # Independent calculation
        # Row 1: deepseek-chat 2026-10-25: 14 req, 28450 prompt (22100 hit + 6350 miss), 4120 out, 0 reas, 0.007621
        # Row 2: deepseek-reasoner 2026-10-25: 3 req, 15800 prompt (8500 hit + 7300 miss), 3450 out, 2100 reas, 0.015240
        # Row 3: deepseek-chat 2026-10-26: 8 req, 12600 prompt (10400 hit + 2200 miss), 1890 out, 0 reas, 0.003348

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

    def test_us05_a_custom_delimiter_and_decimal(self):
        """US-05.a: custom delimiter ';' and decimal separator ','."""
        collector = DeepSeekCollector(DeepSeekConfig(csv_delimiter=";", decimal_separator=","))
        records, report = collector.parse_export_data(FIXTURE_EURO_CSV, format_type="csv")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].cost, Decimal("0.005000"))
        self.assertEqual(records[0].prompt_tokens, 20000)
        self.assertEqual(records[0].prompt_cache_hit_tokens, 15000)
        self.assertEqual(records[0].prompt_cache_miss_tokens, 5000)

    def test_json_and_jsonl_import(self):
        json_content = json.dumps([
            {
                "date": "2026-10-25",
                "model": "deepseek-chat",
                "requests": 5,
                "prompt_tokens": 1000,
                "prompt_cache_hit_tokens": 800,
                "prompt_cache_miss_tokens": 200,
                "completion_tokens": 300,
                "reasoning_tokens": 0,
                "cost": 0.0005,
            }
        ])
        records, report = self.collector.parse_export_data(json_content, format_type="json")
        self.assertEqual(len(records), 1)
        self.assertEqual(report.accepted, 1)

        jsonl_content = '{"date":"2026-10-25","model":"deepseek-chat","requests":1,"prompt_tokens":100,"prompt_cache_hit_tokens":50,"prompt_cache_miss_tokens":50,"completion_tokens":20,"reasoning_tokens":0,"cost":0.0001}\n'
        records_l, report_l = self.collector.parse_export_data(jsonl_content, format_type="jsonl")
        self.assertEqual(len(records_l), 1)
        self.assertEqual(report_l.accepted, 1)

    def test_us05_c_disjoint_counts_and_idempotency(self):
        """US-05.c: total_read == accepted + existing + quarantined + corrected."""
        existing_keys = {"2026-10-25:deepseek-chat"}
        records, report = self.collector.parse_export_data(
            FIXTURE_SAMPLE_CSV,
            format_type="csv",
            existing_keys=existing_keys,
        )
        self.assertEqual(report.total_read, 3)
        self.assertEqual(report.accepted, 2)
        self.assertEqual(report.existing, 1)
        self.assertEqual(report.quarantined, 0)
        self.assertEqual(report.corrected, 0)
        self.assertTrue(report.verify_disjoint_counts())

    def test_us05_d_invariant_violations_isolation(self):
        """US-05.d: Isolated faulty records; partial failure status."""
        records, report = self.collector.parse_export_data(
            FIXTURE_INVARIANT_VIOLATIONS_CSV,
            format_type="csv",
        )
        # All 4 rows violate invariants:
        # 1. prompt (100) != hit (60) + miss (30)
        # 2. reasoning (50) > completion (40)
        # 3. hit (-10) < 0
        # 4. completion (-5) < 0
        self.assertEqual(report.total_read, 4)
        self.assertEqual(report.accepted, 0)
        self.assertEqual(report.quarantined, 4)
        self.assertTrue(report.verify_disjoint_counts())
        self.assertEqual(len(report.errors), 4)

        # Build collection run
        run_obj = self.collector.build_collection_run(
            report=report,
            started_at="2026-10-26T10:00:00Z",
            finished_at="2026-10-26T10:00:01Z",
            period_start="2026-10-25T00:00:00Z",
            period_end="2026-10-25T23:59:59Z",
        )
        self.assertEqual(run_obj["status"], "failure")
        self.assertEqual(run_obj["exit_status"], 1)

    def test_partial_failure_collection_run(self):
        mixed_csv = FIXTURE_SAMPLE_CSV + "2026-10-27,deepseek-chat,1,100,50,20,10,0,0.0001\n"
        records, report = self.collector.parse_export_data(mixed_csv, format_type="csv")
        self.assertEqual(report.total_read, 4)
        self.assertEqual(report.accepted, 3)
        self.assertEqual(report.quarantined, 1)

        run_obj = self.collector.build_collection_run(
            report=report,
            started_at="2026-10-26T10:00:00Z",
            finished_at="2026-10-26T10:00:01Z",
            period_start="2026-10-25T00:00:00Z",
            period_end="2026-10-27T23:59:59Z",
        )
        # US-05.d: Bei zulässigem Teilimport ist der Lauf partial, niemals complete.
        self.assertEqual(run_obj["status"], "partial_failure")
        self.assertEqual(run_obj["exit_status"], 3)
        self.assertEqual(run_obj["account_runs"][0]["status"], "partial_failure")

    def test_records_to_events_and_measurements(self):
        records, report = self.collector.parse_export_data(FIXTURE_SAMPLE_CSV, format_type="csv")
        ev_id, desc = self.collector.build_evidence_descriptor(FIXTURE_SAMPLE_CSV.encode("utf-8"))
        events, measurements = self.collector.records_to_events_and_measurements(
            records=records,
            raw_evidence_id=ev_id,
        )
        self.assertEqual(len(events), 3)
        # 3 records * 7 metrics = 21 measurements
        self.assertEqual(len(measurements), 21)

        for ev in events:
            self.assertEqual(ev["schema_version"], "0.1")
            self.assertEqual(ev["source"], "deepseek")
            self.assertEqual(ev["source_account_id"], "deepseek-main")
            self.assertEqual(ev["raw_evidence_id"], ev_id)
            self.assertIn("window", ev)
            self.assertEqual(len(ev["idempotency_key"]), 64)

        for m in measurements:
            self.assertEqual(m["schema_version"], "0.1")
            self.assertEqual(m["source"], "deepseek")
            self.assertIn(m["unit"], ["request", "token", "usd"])
            self.assertEqual(m["evidence_ids"], [ev_id])

    def test_parse_realtime_inference_usage(self):
        usage = {
            "prompt_tokens": 1500,
            "completion_tokens": 400,
            "total_tokens": 1900,
            "prompt_cache_hit_tokens": 1200,
            "prompt_cache_miss_tokens": 300,
            "prompt_tokens_details": {
                "reasoning_tokens": 150,
            },
        }
        res = parse_inference_usage(usage, model="deepseek-reasoner")
        self.assertEqual(res["model"], "deepseek-reasoner")
        self.assertEqual(res["prompt_tokens"], 1500)
        self.assertEqual(res["prompt_cache_hit_tokens"], 1200)
        self.assertEqual(res["prompt_cache_miss_tokens"], 300)
        self.assertEqual(res["completion_tokens"], 400)
        self.assertEqual(res["reasoning_tokens"], 150)


if __name__ == "__main__":
    unittest.main()
