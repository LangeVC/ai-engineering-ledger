# collectors/backfill — Contracts & Specifications

## Overview
`collectors/backfill` manages repeatable backfills, interruptible pagination runs, cursor states, idempotency controls, and partial coverage tracking for the AI Engineering Ledger (AEL).

## Scope & Compliance
- **US-06.a**: Replaying the same import file or backfilling the same provider IDs results in **0 new active measurements** and unchanged totals. Duplicate attempts remain auditable and logged (`existing` count incremented, logged in audit trail).
- **US-06.b**: Controlled continuation after interrupted pagination runs (e.g. simulated network disconnect between pages). Resulting data exactly matches an uninterrupted run. The progress marker / resume cursor NEVER exceeds the last securely committed page/batch.
- **US-06.c**: Overlapping exports and revisions adhere to Section 9:
  1. Identical artifacts (same content hash) are deduplicated while preserving source/account bindings.
  2. Stable provider/session IDs govern event identity. In the absence of a reliable identity, events sharing identical timestamp and token count are NOT merged without an explicit differentiator.
  3. Revisions (changed values for identical scope and window) supersede prior values rather than accumulating as independent consumption events.
- **US-06.d / Coverage Tracking**: Distinct accounting and tracking of **Requested Periods**, **Delivered Periods**, and **Confirmed Complete Periods**. Empty responses or the oldest record found do NOT prove full historical coverage.

## Core Classes
- `CursorPaginationState`: Tracks page tokens, cursors, record counts, and last committed checkpoint.
- `CoverageLedger`: Tracks intervals requested, delivered, and confirmed complete. Detects gaps and unverified windows.
- `IdempotencyManager`: Computes and checks DQ03 reference idempotency keys, manages duplicate rejection and audit logging.
- `BackfillManager`: High-level controller orchestrating paginated fetches, atomic persistence checkpoints, interrupt-and-resume workflows, and revision handling.
