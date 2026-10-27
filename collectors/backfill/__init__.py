"""
collectors/backfill
Repeatable backfills, crash recovery, cursor pagination, and partial coverage tracking.
"""

from .manager import (
    BackfillBatchResult,
    BackfillManager,
    BackfillStore,
    CoverageLedger,
    CoverageReport,
    CursorPaginationState,
    IdempotencyManager,
    PeriodCoverage,
    compute_sha256,
    derive_idempotency_key,
    generate_ulid,
)

__all__ = [
    "BackfillBatchResult",
    "BackfillManager",
    "BackfillStore",
    "CoverageLedger",
    "CoverageReport",
    "CursorPaginationState",
    "IdempotencyManager",
    "PeriodCoverage",
    "compute_sha256",
    "derive_idempotency_key",
    "generate_ulid",
]
