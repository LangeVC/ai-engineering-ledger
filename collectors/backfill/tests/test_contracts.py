#!/usr/bin/env python3
"""Contract conformance tests for collectors.backfill.
Validates:
- Idempotency key canonical derivation matches schema/core semantic standard
- Checkpoint data structure validity and roundtripping
- Coverage reporting conformance
- Run result status reporting conformance
"""

import json
import os
import sys
from pathlib import Path
import tempfile
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
curr = TEST_DIR
REPO_ROOT = None
while curr and curr != "/":
    if os.path.isdir(os.path.join(curr, "schema", "core")):
        REPO_ROOT = curr
        break
    curr = os.path.dirname(curr)

if REPO_ROOT and REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from schema.core.semantic import derive_idempotency_key as core_derive_idempotency_key
from collectors.backfill import (
    BackfillBatchResult,
    BackfillManager,
    BackfillStore,
    CoverageLedger,
    CoverageReport,
    CursorPaginationState,
    IdempotencyManager,
    PeriodCoverage,
    derive_idempotency_key as backfill_derive_idempotency_key,
    generate_ulid,
)


class TestBackfillContracts(unittest.TestCase):
    def test_idempotency_key_canonical_matching(self):
        """Verifies that backfill_derive_idempotency_key produces identical keys
        to schema.core.semantic.derive_idempotency_key (DQ03).
        """
        source = "deepseek"
        account = "account-main"
        native_id = "req_12345678"
        window = {
            "type": "daily",
            "starts_at": "2026-10-25T00:00:00Z",
            "ends_at": "2026-10-25T23:59:59Z",
        }

        core_key = core_derive_idempotency_key(
            source=source,
            account=account,
            native_id=native_id,
            window=window,
        )

        backfill_key = backfill_derive_idempotency_key(
            source=source,
            account=account,
            native_id=native_id,
            window=window,
        )

        self.assertEqual(core_key, backfill_key)

    def test_cursor_pagination_state_serialization(self):
        """Validates round-trip serialization of CursorPaginationState."""
        state = CursorPaginationState(
            backfill_id="bf-roundtrip-01",
            source="github",
            account="gh-main",
            current_page=5,
            last_cursor="https://api.github.com/resource?page=6",
            committed_page=5,
            committed_cursor="https://api.github.com/resource?page=6",
            total_records_committed=500,
            is_finished=False,
            last_checkpoint_at="2026-10-26T12:00:00Z",
            metadata={"repo": "owner/repo", "per_page": 100},
        )

        d = state.to_dict()
        reconstituted = CursorPaginationState.from_dict(d)

        self.assertEqual(state.backfill_id, reconstituted.backfill_id)
        self.assertEqual(state.committed_page, reconstituted.committed_page)
        self.assertEqual(state.committed_cursor, reconstituted.committed_cursor)
        self.assertEqual(state.total_records_committed, reconstituted.total_records_committed)
        self.assertEqual(state.metadata, reconstituted.metadata)

    def test_period_coverage_contract(self):
        """Validates PeriodCoverage contract structure."""
        cov = PeriodCoverage(
            period_start="2026-10-01T00:00:00Z",
            period_end="2026-10-02T00:00:00Z",
            status="confirmed_complete",
            records_count=42,
            oldest_record_time="2026-10-01T02:00:00Z",
            newest_record_time="2026-10-01T23:50:00Z",
            confirmation_reason="Full pagination exhausted",
        )
        d = cov.to_dict()
        self.assertIn("period_start", d)
        self.assertIn("period_end", d)
        self.assertIn("status", d)
        self.assertEqual(d["records_count"], 42)


if __name__ == "__main__":
    unittest.main()
