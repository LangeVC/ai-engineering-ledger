#!/usr/bin/env python3
"""Negative probe for AEL-US08-02 (US-08.d and related functionality).
Fails if new US-08.d methods are not yet present on BytePlusCollector.
"""
import os
import sys
import unittest

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

class TestBytePlusUS08Part2Probe(unittest.TestCase):
    def test_methods_exist(self):
        from collectors.byteplus.collector import BytePlusCollector
        collector = BytePlusCollector()
        self.assertTrue(hasattr(collector, "collect_paginated_billing"), "BytePlusCollector must support collect_paginated_billing")
        self.assertTrue(hasattr(collector, "reconcile_superseding_billing"), "BytePlusCollector must support reconcile_superseding_billing")
        self.assertTrue(hasattr(collector, "compare_historical_snapshots"), "BytePlusCollector must support compare_historical_snapshots")
        self.assertTrue(hasattr(collector, "detect_quota_reset_or_renewal"), "BytePlusCollector must support detect_quota_reset_or_renewal")

if __name__ == "__main__":
    unittest.main()
