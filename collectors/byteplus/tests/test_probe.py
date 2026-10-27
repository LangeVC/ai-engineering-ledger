#!/usr/bin/env python3
"""Negative failure probe for AEL-US08-01.
Verifies that without the BytePlus collector implementation, imports fail.
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

class TestBytePlusNegativeProbe(unittest.TestCase):
    def test_collector_exists(self):
        try:
            from collectors.byteplus.collector import BytePlusCollector
            present = True
        except ImportError:
            present = False
        self.assertTrue(present, "BytePlusCollector must be implemented (Negative probe: should fail before implementation)")

if __name__ == "__main__":
    unittest.main()
