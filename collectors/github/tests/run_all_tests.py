#!/usr/bin/env python3
"""Runner for all unit, contract, and integration tests in collectors/github."""

import os
import sys
import unittest

if __name__ == "__main__":
    start_dir = os.path.dirname(__file__)
    suite = unittest.defaultTestLoader.discover(start_dir, pattern="test_*.py")
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
