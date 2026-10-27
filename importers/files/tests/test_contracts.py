#!/usr/bin/env python3
"""Contract conformance test for importers/files.
Validates generated events and measurements from file import against schema/core validators.
"""

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
    REPO_ROOT = os.path.abspath(os.path.join(TEST_DIR, "../../../../../.."))

WORKTREE_ROOT = os.path.abspath(os.path.join(TEST_DIR, "../../.."))

sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, WORKTREE_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "schema", "core"))
sys.path.insert(0, os.path.join(REPO_ROOT, "schema", "core", "tests"))

from importers.files import FileImporter, FormatOptions
import minival
import semantic

L_CORE = minival.Loader(os.path.join(REPO_ROOT, "schema", "core"))
CAT_CORE = L_CORE.doc("units.catalog.json")

SAMPLE_CSV = """date,model,requests,prompt_tokens,prompt_cache_hit_tokens,prompt_cache_miss_tokens,completion_tokens,reasoning_tokens,cost
2026-10-25,deepseek-chat,14,28450,22100,6350,4120,0,0.007621
2026-10-25,deepseek-reasoner,3,15800,8500,7300,3450,2100,0.015240
"""


class TestFileImporterContracts(unittest.TestCase):

    def setUp(self):
        self.importer = FileImporter()

    def test_generated_events_and_measurements_conformance(self):
        """Validates that events and measurements produced by FileImporter conform to schema/core contracts."""
        events, measurements, report = self.importer.import_deepseek_export(SAMPLE_CSV)

        self.assertEqual(len(events), 2)
        self.assertTrue(len(measurements) > 0)

        # Validate events
        for ev in events:
            errs = minival.validate(ev, "event.schema.json", L_CORE)
            self.assertEqual(errs, [], f"Event failed schema validation: {errs}")
            sem_errs = semantic.check_event(ev)
            self.assertEqual(sem_errs, [], f"Event failed semantic validation: {sem_errs}")

        # Validate measurements
        for m in measurements:
            errs = minival.validate(m, "measurement.schema.json", L_CORE)
            self.assertEqual(errs, [], f"Measurement failed schema validation: {errs}")
            sem_errs = semantic.check_measurement(m, CAT_CORE)
            self.assertEqual(sem_errs, [], f"Measurement failed semantic validation: {sem_errs}")


if __name__ == "__main__":
    unittest.main()
