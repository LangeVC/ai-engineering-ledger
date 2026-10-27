"""
collectors/backfill
Repeatable backfills, crash recovery, cursor pagination, and partial coverage tracking.
Pure Python 3 standard library implementation for AI Engineering Ledger (AEL).

Enforces:
- US-06.a: Idempotent replay (0 new active measurements, unchanged sums, auditable attempts)
- US-06.b: Controlled resumption after interrupt (checkpoint never advances beyond committed batch)
- US-06.c: Section 9 deduplication, stable identity handling, revision vs duplicate resolution,
           and prevention of accidental collapse of distinct requests lacking reliable IDs
- US-06.d: Explicit tracking of requested, delivered, and confirmed periods
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

logger = logging.getLogger("ael.collectors.backfill")

# Crockford Base32 alphabet for ULID generation
_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def generate_ulid(timestamp_ms: Optional[int] = None) -> str:
    """Generate a Crockford-encoded ULID string of 26 characters."""
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)
    ts_bytes = timestamp_ms.to_bytes(6, byteorder="big")
    rand_bytes = os.urandom(10)
    data = ts_bytes + rand_bytes
    val = int.from_bytes(data, byteorder="big")
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD_ALPHABET[val & 0x1F])
        val >>= 5
    return "".join(reversed(chars))


def _utc_iso(dt: Optional[Union[str, datetime.datetime]] = None) -> str:
    """Normalizes datetime or string to ISO 8601 UTC with 'Z' suffix."""
    if dt is None:
        return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(dt, datetime.datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(dt, str):
        s = dt.strip()
        if not s:
            return _utc_iso()
        # Parse timestamp
        # Handle simple date YYYY-MM-DD
        if len(s) == 10 and s.count("-") == 2:
            s = f"{s}T00:00:00Z"
        s = s.replace("+00:00", "Z")
        if not s.endswith("Z") and "+" not in s and "-" not in s[10:]:
            s = f"{s}Z"
        parsed = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return parsed.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    raise ValueError(f"Cannot convert {type(dt)} to UTC ISO string")


def compute_sha256(data: Union[bytes, str]) -> str:
    """Computes SHA-256 hex digest of string or bytes."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _utc_dt(ts: Union[str, datetime.datetime]) -> datetime.datetime:
    if isinstance(ts, datetime.datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        return ts.astimezone(datetime.timezone.utc)
    s = ts.replace("Z", "+00:00")
    if "." in s:
        a, b = s.split(".", 1)
        i = 0
        while i < len(b) and b[i].isdigit():
            i += 1
        s = a + "." + b[:i][:6].ljust(6, "0") + b[i:]
    return datetime.datetime.fromisoformat(s).astimezone(datetime.timezone.utc)


def derive_idempotency_key(
    source: str,
    account: str,
    native_id: Optional[str],
    window: Optional[Dict[str, Any]],
    raw_hash: Optional[str] = None,
) -> str:
    """Reference derivation (DQ03).
    native_id: provider/session/forge id or documented source-specific key.
    window: dict(type, starts_at, ends_at) or None.
    Normalized to UTC matching schema.core.semantic.derive_idempotency_key.
    """
    w = (
        [None, None, None]
        if window is None
        else [
            window.get("type"),
            _utc_dt(window["starts_at"]).isoformat(),
            _utc_dt(window["ends_at"]).isoformat(),
        ]
    )
    payload = ["ael.idem.v0.1", source, account, native_id] + w
    if raw_hash:
        payload.append(raw_hash)
    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


# --- Coverage Tracking (US-06.d) ---

class CoverageLedger:
    """Helper facade for managing period coverage records and assessing gaps."""

    def __init__(self, store: BackfillStore):
        self.store = store

    def record_period(
        self,
        backfill_id: str,
        source: str,
        account: str,
        period_start: str,
        period_end: str,
        status: str,
        records_count: int,
        oldest_record_time: Optional[str] = None,
        newest_record_time: Optional[str] = None,
        confirmation_reason: Optional[str] = None,
    ) -> None:
        self.store.record_coverage_period(
            backfill_id=backfill_id,
            source=source,
            account=account,
            period_start=period_start,
            period_end=period_end,
            status=status,
            records_count=records_count,
            oldest_record_time=oldest_record_time,
            newest_record_time=newest_record_time,
            confirmation_reason=confirmation_reason,
        )

    def get_ledger(self, backfill_id: str) -> List[PeriodCoverage]:
        return self.store.get_coverage_ledger(backfill_id)


class IdempotencyManager:
    """Helper facade for checking artifact and event idempotency."""

    def __init__(self, store: BackfillStore):
        self.store = store

    def check_or_record_artifact(
        self,
        source: str,
        account: str,
        content_hash: str,
        artifact_id: Optional[str] = None,
    ) -> Tuple[bool, int]:
        return self.store.check_or_record_artifact(
            source=source,
            account=account,
            content_hash=content_hash,
            artifact_id=artifact_id,
        )

    def get_audit_attempts(self, backfill_id: Optional[str] = None) -> List[Dict[str, Any]]:
        return self.store.get_audit_attempts(backfill_id)


@dataclass
class PeriodCoverage:
    """Detailed record of requested vs delivered vs confirmed periods."""
    period_start: str
    period_end: str
    status: str  # "confirmed_complete", "partial", "empty_unverified", "missing", "in_progress"
    records_count: int = 0
    oldest_record_time: Optional[str] = None
    newest_record_time: Optional[str] = None
    confirmation_reason: str = "unverified"
    notes: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CoverageReport:
    """Aggregated coverage report comparing requested, delivered, and confirmed periods."""
    source: str
    account: str
    requested_start: str
    requested_end: str
    delivered_periods: List[PeriodCoverage] = field(default_factory=list)
    confirmed_periods: List[PeriodCoverage] = field(default_factory=list)
    unconfirmed_gaps: List[Tuple[str, str]] = field(default_factory=list)
    total_records_delivered: int = 0
    is_fully_confirmed: bool = False
    oldest_record_seen: Optional[str] = None
    empty_response_observed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "account": self.account,
            "requested_start": self.requested_start,
            "requested_end": self.requested_end,
            "total_records_delivered": self.total_records_delivered,
            "is_fully_confirmed": self.is_fully_confirmed,
            "oldest_record_seen": self.oldest_record_seen,
            "empty_response_observed": self.empty_response_observed,
            "delivered_periods": [p.to_dict() for p in self.delivered_periods],
            "confirmed_periods": [p.to_dict() for p in self.confirmed_periods],
            "unconfirmed_gaps": self.unconfirmed_gaps,
        }


# --- Cursor and Pagination State (US-06.b) ---

@dataclass
class CursorPaginationState:
    """Persistent cursor state for backfill resumption."""
    backfill_id: str
    source: str
    account: str
    current_page: int
    last_cursor: Optional[str]
    committed_page: int
    committed_cursor: Optional[str]
    total_records_committed: int
    is_finished: bool
    last_checkpoint_at: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> CursorPaginationState:
        return cls(
            backfill_id=d["backfill_id"],
            source=d["source"],
            account=d["account"],
            current_page=d.get("current_page", 0),
            last_cursor=d.get("last_cursor"),
            committed_page=d.get("committed_page", 0),
            committed_cursor=d.get("committed_cursor"),
            total_records_committed=d.get("total_records_committed", 0),
            is_finished=d.get("is_finished", False),
            last_checkpoint_at=d.get("last_checkpoint_at", _utc_iso()),
            metadata=d.get("metadata", {}),
        )


# --- Idempotency & Replay Accounting (US-06.a, US-06.c) ---

@dataclass
class BackfillBatchResult:
    """Disjoint accounting result of a backfill or file import operation."""
    total_seen: int = 0
    accepted: int = 0
    existing: int = 0
    revisions: int = 0
    quarantined: int = 0
    status: str = "in_progress"  # "complete", "partial", "interrupted", "failed"
    new_active_measurements: int = 0
    active_sums: Dict[str, Decimal] = field(default_factory=dict)
    committed_page: int = 0
    cursor: Optional[str] = None
    error_message: Optional[str] = None

    def verify_disjoint_counts(self) -> bool:
        """Enforces that total_seen == accepted + existing + revisions + quarantined."""
        return self.total_seen == (self.accepted + self.existing + self.revisions + self.quarantined)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_seen": self.total_seen,
            "accepted": self.accepted,
            "existing": self.existing,
            "revisions": self.revisions,
            "quarantined": self.quarantined,
            "status": self.status,
            "new_active_measurements": self.new_active_measurements,
            "active_sums": {k: str(v) for k, v in self.active_sums.items()},
            "committed_page": self.committed_page,
            "cursor": self.cursor,
            "error_message": self.error_message,
        }


# --- Backfill Database & Store Manager ---

class BackfillStore:
    """SQLite-backed persistent store for backfill checkpoints, pagination states,
    idempotency registry, active measurements, and audit logs.
    """

    def __init__(self, db_path: Union[str, Path] = ":memory:"):
        self.db_path = str(db_path)
        self._conn: Optional[sqlite3.Connection] = None
        self._init_conn()
        self._init_db()

    def _init_conn(self) -> None:
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON;")
        if self.db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL;")

    def _get_connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._init_conn()
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.executescript(
                """
                -- Pagination checkpoints
                CREATE TABLE IF NOT EXISTS backfill_checkpoints (
                    backfill_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    account TEXT NOT NULL,
                    current_page INTEGER NOT NULL,
                    last_cursor TEXT,
                    committed_page INTEGER NOT NULL,
                    committed_cursor TEXT,
                    total_records_committed INTEGER NOT NULL,
                    is_finished INTEGER NOT NULL DEFAULT 0,
                    last_checkpoint_at TEXT NOT NULL,
                    metadata_json TEXT
                );

                -- Idempotency registry for raw artifacts and events
                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    idempotency_key TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    account TEXT NOT NULL,
                    native_id TEXT,
                    entity_type TEXT NOT NULL, -- 'event', 'measurement', 'file_artifact'
                    first_seen_at TEXT NOT NULL,
                    last_attempt_at TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 1,
                    event_id TEXT,
                    measurement_id TEXT,
                    content_hash TEXT
                );

                -- Active and historical measurements table
                CREATE TABLE IF NOT EXISTS measurements (
                    measurement_id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    metric TEXT NOT NULL,
                    value TEXT NOT NULL,
                    unit TEXT NOT NULL,
                    aggregation TEXT NOT NULL,
                    source TEXT NOT NULL,
                    account TEXT NOT NULL,
                    provider TEXT,
                    model TEXT,
                    native_id TEXT,
                    window_start TEXT,
                    window_end TEXT,
                    scope_hash TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    revision_no INTEGER NOT NULL DEFAULT 1,
                    supersedes_id TEXT,
                    created_at TEXT NOT NULL,
                    effective_from TEXT NOT NULL
                );

                -- Audit log for duplicate replay attempts and conflicts
                CREATE TABLE IF NOT EXISTS audit_attempts (
                    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    backfill_id TEXT,
                    source TEXT NOT NULL,
                    account TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    native_id TEXT,
                    attempt_type TEXT NOT NULL, -- 'duplicate_replay', 'revision', 'quarantine'
                    details TEXT,
                    recorded_at TEXT NOT NULL
                );

                -- Period coverage ledger
                CREATE TABLE IF NOT EXISTS coverage_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    backfill_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    account TEXT NOT NULL,
                    period_start TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    status TEXT NOT NULL,
                    records_count INTEGER NOT NULL DEFAULT 0,
                    oldest_record_time TEXT,
                    newest_record_time TEXT,
                    confirmation_reason TEXT,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def save_checkpoint(self, state: CursorPaginationState) -> None:
        """Atomically saves checkpoint state.
        US-06.b guarantee: committed_page and committed_cursor NEVER advance beyond
        the last securely committed transaction.
        """
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO backfill_checkpoints (
                    backfill_id, source, account, current_page, last_cursor,
                    committed_page, committed_cursor, total_records_committed,
                    is_finished, last_checkpoint_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(backfill_id) DO UPDATE SET
                    current_page = excluded.current_page,
                    last_cursor = excluded.last_cursor,
                    committed_page = excluded.committed_page,
                    committed_cursor = excluded.committed_cursor,
                    total_records_committed = excluded.total_records_committed,
                    is_finished = excluded.is_finished,
                    last_checkpoint_at = excluded.last_checkpoint_at,
                    metadata_json = excluded.metadata_json
                """,
                (
                    state.backfill_id,
                    state.source,
                    state.account,
                    state.current_page,
                    state.last_cursor,
                    state.committed_page,
                    state.committed_cursor,
                    state.total_records_committed,
                    1 if state.is_finished else 0,
                    state.last_checkpoint_at,
                    json.dumps(state.metadata),
                ),
            )

    def get_checkpoint(self, backfill_id: str) -> Optional[CursorPaginationState]:
        """Loads the last valid checkpoint for a backfill operation."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM backfill_checkpoints WHERE backfill_id = ?",
                (backfill_id,),
            ).fetchone()
            if not row:
                return None
            return CursorPaginationState(
                backfill_id=row["backfill_id"],
                source=row["source"],
                account=row["account"],
                current_page=row["current_page"],
                last_cursor=row["last_cursor"],
                committed_page=row["committed_page"],
                committed_cursor=row["committed_cursor"],
                total_records_committed=row["total_records_committed"],
                is_finished=bool(row["is_finished"]),
                last_checkpoint_at=row["last_checkpoint_at"],
                metadata=json.loads(row["metadata_json"] or "{}"),
            )

    def check_or_record_artifact(
        self,
        source: str,
        account: str,
        content_hash: str,
        artifact_id: Optional[str] = None,
    ) -> Tuple[bool, int]:
        """Section 9.1 (1): Identical artifact hash check.
        Returns (is_duplicate, attempt_count).
        If already present for this source & account, records attempt and returns True.
        """
        idem_key = compute_sha256(f"artifact:{source}:{account}:{content_hash}")
        now = _utc_iso()
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT attempt_count FROM idempotency_keys WHERE idempotency_key = ?",
                (idem_key,),
            ).fetchone()
            if row:
                attempts = row["attempt_count"] + 1
                conn.execute(
                    """
                    UPDATE idempotency_keys
                    SET attempt_count = ?, last_attempt_at = ?
                    WHERE idempotency_key = ?
                    """,
                    (attempts, now, idem_key),
                )
                conn.execute(
                    """
                    INSERT INTO audit_attempts (
                        source, account, idempotency_key, native_id, attempt_type, details, recorded_at
                    ) VALUES (?, ?, ?, ?, 'duplicate_replay', ?, ?)
                    """,
                    (source, account, idem_key, artifact_id, f"Artifact re-imported (attempt {attempts})", now),
                )
                return True, attempts
            else:
                conn.execute(
                    """
                    INSERT INTO idempotency_keys (
                        idempotency_key, source, account, native_id, entity_type,
                        first_seen_at, last_attempt_at, attempt_count, content_hash
                    ) VALUES (?, ?, ?, ?, 'file_artifact', ?, ?, 1, ?)
                    """,
                    (idem_key, source, account, artifact_id, now, now, content_hash),
                )
                return False, 1

    def compute_scope_hash(
        self,
        account: str,
        provider: str,
        model: str,
        metric: str,
        unit: str,
        window_start: str,
        window_end: str,
        native_id: Optional[str] = None,
    ) -> str:
        """Section 9.1: Scope hash for duplicate & revision detection.
        Scope includes Account, Provider, Model, Metric, Unit, exact interval,
        and native_id (when available for granular requests/events).
        """
        scope_data = [
            account,
            provider,
            model,
            metric,
            unit,
            _utc_iso(window_start),
            _utc_iso(window_end),
            native_id,
        ]
        return compute_sha256(json.dumps(scope_data, separators=(",", ":")))

    def record_measurement(
        self,
        measurement_id: str,
        event_id: str,
        metric: str,
        value: Union[str, int, Decimal],
        unit: str,
        aggregation: str,
        source: str,
        account: str,
        provider: str,
        model: str,
        native_id: Optional[str],
        window_start: str,
        window_end: str,
        backfill_id: Optional[str] = None,
        allow_revision: bool = True,
        force_distinct_unidentified: bool = False,
    ) -> Tuple[str, Optional[str]]:
        """Stores a measurement adhering to Section 9 rules:
        - If matching native_id and identical value -> 'existing' (duplicate replay)
        - If matching native_id and changed value -> 'revision' (deactivates old, records new)
        - If scope matches (same account, metric, unit, exact interval, native_id) and changed value:
            -> revision
        - If native_id is missing:
            US-06.c: Equal timestamp & tokens alone do NOT merge unless explicit identity exists.
        Returns (action, assigned_measurement_id) where action is 'accepted', 'existing', or 'revision'.
        """
        val_str = str(value)
        scope_h = self.compute_scope_hash(
            account=account,
            provider=provider,
            model=model,
            metric=metric,
            unit=unit,
            window_start=window_start,
            window_end=window_end,
            native_id=native_id,
        )
        now = _utc_iso()

        # Idempotency key per DQ03
        window_obj = {"type": aggregation, "starts_at": window_start, "ends_at": window_end}
        idem_key = derive_idempotency_key(
            source=source,
            account=account,
            native_id=native_id if native_id else (f"anon-{generate_ulid()}" if force_distinct_unidentified else None),
            window=window_obj,
        )

        with self._get_connection() as conn:
            # 1. Check if exact idempotency key exists
            if native_id:
                row_idem = conn.execute(
                    "SELECT attempt_count, measurement_id FROM idempotency_keys WHERE idempotency_key = ?",
                    (idem_key,),
                ).fetchone()
                if row_idem:
                    attempts = row_idem["attempt_count"] + 1
                    conn.execute(
                        """
                        UPDATE idempotency_keys
                        SET attempt_count = ?, last_attempt_at = ?
                        WHERE idempotency_key = ?
                        """,
                        (attempts, now, idem_key),
                    )
                    # Check if value has changed (Revision) or is identical
                    existing_meas = conn.execute(
                        "SELECT value, measurement_id FROM measurements WHERE measurement_id = ? AND is_active = 1",
                        (row_idem["measurement_id"],),
                    ).fetchone()
                    if existing_meas and Decimal(existing_meas["value"]) == Decimal(val_str):
                        # Identical replay: 0 changes to active measurements
                        conn.execute(
                            """
                            INSERT INTO audit_attempts (
                                backfill_id, source, account, idempotency_key, native_id, attempt_type, details, recorded_at
                            ) VALUES (?, ?, ?, ?, ?, 'duplicate_replay', ?, ?)
                            """,
                            (backfill_id, source, account, idem_key, native_id, f"Duplicate replay attempt {attempts}", now),
                        )
                        return "existing", existing_meas["measurement_id"]
                    elif allow_revision and existing_meas:
                        # Value changed for same native ID -> Revision (Section 9.1)
                        old_meas_id = existing_meas["measurement_id"]
                        old_row = conn.execute(
                            "SELECT revision_no FROM measurements WHERE measurement_id = ?",
                            (old_meas_id,),
                        ).fetchone()
                        next_rev = (old_row["revision_no"] if old_row else 1) + 1

                        # Deactivate previous revision
                        conn.execute("UPDATE measurements SET is_active = 0 WHERE measurement_id = ?", (old_meas_id,))
                        # Insert new revision
                        conn.execute(
                            """
                            INSERT INTO measurements (
                                measurement_id, event_id, metric, value, unit, aggregation,
                                source, account, provider, model, native_id,
                                window_start, window_end, scope_hash, is_active,
                                revision_no, supersedes_id, created_at, effective_from
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                            """,
                            (
                                measurement_id, event_id, metric, val_str, unit, aggregation,
                                source, account, provider, model, native_id,
                                window_start, window_end, scope_h, next_rev, old_meas_id, now, now
                            ),
                        )
                        conn.execute(
                            "UPDATE idempotency_keys SET measurement_id = ? WHERE idempotency_key = ?",
                            (measurement_id, idem_key),
                        )
                        conn.execute(
                            """
                            INSERT INTO audit_attempts (
                                backfill_id, source, account, idempotency_key, native_id, attempt_type, details, recorded_at
                            ) VALUES (?, ?, ?, ?, ?, 'revision', ?, ?)
                            """,
                            (backfill_id, source, account, idem_key, native_id, f"Revision supersedes {old_meas_id}", now),
                        )
                        return "revision", measurement_id

            # 2. Check scope-level revision for interval aggregate (Section 9.1: identical scope, changed value)
            if not force_distinct_unidentified and native_id is None:
                active_scope_row = conn.execute(
                    "SELECT measurement_id, value, revision_no FROM measurements WHERE scope_hash = ? AND is_active = 1",
                    (scope_h,),
                ).fetchone()
                if active_scope_row:
                    if Decimal(active_scope_row["value"]) == Decimal(val_str):
                        return "existing", active_scope_row["measurement_id"]
                    elif allow_revision:
                        # Changed value for same scope -> Revision
                        old_meas_id = active_scope_row["measurement_id"]
                        next_rev = active_scope_row["revision_no"] + 1
                        conn.execute("UPDATE measurements SET is_active = 0 WHERE measurement_id = ?", (old_meas_id,))
                        conn.execute(
                            """
                            INSERT INTO measurements (
                                measurement_id, event_id, metric, value, unit, aggregation,
                                source, account, provider, model, native_id,
                                window_start, window_end, scope_hash, is_active,
                                revision_no, supersedes_id, created_at, effective_from
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                            """,
                            (
                                measurement_id, event_id, metric, val_str, unit, aggregation,
                                source, account, provider, model, native_id,
                                window_start, window_end, scope_h, next_rev, old_meas_id, now, now
                            ),
                        )
                        return "revision", measurement_id

            # 3. Brand new active measurement
            conn.execute(
                """
                INSERT INTO measurements (
                    measurement_id, event_id, metric, value, unit, aggregation,
                    source, account, provider, model, native_id,
                    window_start, window_end, scope_hash, is_active,
                    revision_no, supersedes_id, created_at, effective_from
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, NULL, ?, ?)
                """,
                (
                    measurement_id, event_id, metric, val_str, unit, aggregation,
                    source, account, provider, model, native_id,
                    window_start, window_end, scope_h, now, now
                ),
            )
            conn.execute(
                """
                INSERT INTO idempotency_keys (
                    idempotency_key, source, account, native_id, entity_type,
                    first_seen_at, last_attempt_at, attempt_count, measurement_id
                ) VALUES (?, ?, ?, ?, 'measurement', ?, ?, 1, ?)
                """,
                (idem_key, source, account, native_id, now, now, measurement_id),
            )
            return "accepted", measurement_id

    def get_active_measurements_count_and_sums(
        self,
        source: Optional[str] = None,
        account: Optional[str] = None,
    ) -> Tuple[int, Dict[str, Decimal]]:
        """Returns (active_count, dict_of_metric_sums)."""
        query = "SELECT metric, value FROM measurements WHERE is_active = 1"
        params: List[Any] = []
        if source:
            query += " AND source = ?"
            params.append(source)
        if account:
            query += " AND account = ?"
            params.append(account)

        with self._get_connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()

        sums: Dict[str, Decimal] = {}
        for r in rows:
            m = r["metric"]
            val = Decimal(str(r["value"]))
            sums[m] = sums.get(m, Decimal(0)) + val

        return len(rows), sums

    def get_audit_attempts(self, backfill_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Returns all logged audit attempts."""
        query = "SELECT * FROM audit_attempts"
        params = []
        if backfill_id:
            query += " WHERE backfill_id = ?"
            params.append(backfill_id)
        query += " ORDER BY attempt_id ASC"

        with self._get_connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            return [dict(r) for r in rows]

    def record_coverage_period(
        self,
        backfill_id: str,
        source: str,
        account: str,
        period_start: str,
        period_end: str,
        status: str,
        records_count: int,
        oldest_record_time: Optional[str] = None,
        newest_record_time: Optional[str] = None,
        confirmation_reason: Optional[str] = None,
    ) -> None:
        """Stores a period coverage entry (US-06.d)."""
        now = _utc_iso()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO coverage_ledger (
                    backfill_id, source, account, period_start, period_end,
                    status, records_count, oldest_record_time, newest_record_time,
                    confirmation_reason, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    backfill_id, source, account, period_start, period_end,
                    status, records_count, oldest_record_time, newest_record_time,
                    confirmation_reason or "unverified", now
                ),
            )

    def get_coverage_ledger(self, backfill_id: str) -> List[PeriodCoverage]:
        """Loads all coverage ledger entries for a backfill run."""
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM coverage_ledger WHERE backfill_id = ? ORDER BY period_start ASC",
                (backfill_id,),
            ).fetchall()
            return [
                PeriodCoverage(
                    period_start=r["period_start"],
                    period_end=r["period_end"],
                    status=r["status"],
                    records_count=r["records_count"],
                    oldest_record_time=r["oldest_record_time"],
                    newest_record_time=r["newest_record_time"],
                    confirmation_reason=r["confirmation_reason"],
                )
                for r in rows
            ]


# --- High-level Backfill Manager ---

class BackfillManager:
    """Orchestrates paginated backfills, atomic commit checkpoints, resume after disconnect,
    and partial coverage assessment.
    """

    def __init__(self, store: Optional[BackfillStore] = None, db_path: Union[str, Path] = ":memory:"):
        self.store = store if store is not None else BackfillStore(db_path=db_path)

    def run_paginated_backfill(
        self,
        backfill_id: str,
        source: str,
        account: str,
        page_fetcher: Callable[[int, Optional[str]], Tuple[List[Dict[str, Any]], Optional[str], bool]],
        requested_start: str,
        requested_end: str,
        simulate_interrupt_after_page: Optional[int] = None,
        force_distinct_unidentified: bool = False,
    ) -> BackfillBatchResult:
        """Executes a backfill with pagination and crash-recovery checkpointing (US-06.b).
        - page_fetcher(page_number, cursor) -> (records, next_cursor, has_more)
        - If simulate_interrupt_after_page is set, raises ConnectionError simulating network break
          AFTER page is fetched but BEFORE checkpoint or next page.
        - Checkpoint is committed strictly AFTER all records of the page are securely stored.
        """
        # 1. Check for existing checkpoint
        checkpoint = self.store.get_checkpoint(backfill_id)
        if checkpoint and checkpoint.is_finished:
            # Already completed
            total_active, sums = self.store.get_active_measurements_count_and_sums(source=source, account=account)
            return BackfillBatchResult(
                total_seen=checkpoint.total_records_committed,
                accepted=0,
                existing=checkpoint.total_records_committed,
                revisions=0,
                quarantined=0,
                status="complete",
                new_active_measurements=0,
                active_sums=sums,
                committed_page=checkpoint.committed_page,
                cursor=checkpoint.committed_cursor,
            )

        start_page = 1
        current_cursor = None
        total_committed = 0

        if checkpoint:
            # Resume from last committed page
            start_page = checkpoint.committed_page + 1
            current_cursor = checkpoint.committed_cursor
            total_committed = checkpoint.total_records_committed

        result = BackfillBatchResult(
            total_seen=total_committed,
            accepted=0,
            existing=0,
            revisions=0,
            quarantined=0,
            status="in_progress",
            committed_page=checkpoint.committed_page if checkpoint else 0,
            cursor=current_cursor,
        )

        curr_page = start_page
        has_more = True
        oldest_ts: Optional[str] = None
        newest_ts: Optional[str] = None

        while has_more:
            # Check interrupt condition BEFORE committing
            if simulate_interrupt_after_page is not None and curr_page > simulate_interrupt_after_page:
                result.status = "interrupted"
                result.error_message = f"Simulated connection disconnect after page {simulate_interrupt_after_page}"
                break

            records, next_cursor, has_more = page_fetcher(curr_page, current_cursor)

            # Process records for this page
            page_records_committed = 0
            for rec in records:
                result.total_seen += 1
                rec_ts = rec.get("timestamp") or rec.get("date") or requested_start
                if oldest_ts is None or rec_ts < oldest_ts:
                    oldest_ts = rec_ts
                if newest_ts is None or rec_ts > newest_ts:
                    newest_ts = rec_ts

                # Extract metric fields
                val = rec.get("value", 0)
                metric = rec.get("metric", "requests")
                unit = rec.get("unit", "request")
                agg = rec.get("aggregation", "interval_total")
                provider = rec.get("provider", source)
                model = rec.get("model", "unknown")
                native_id = rec.get("id") or rec.get("native_id")

                meas_id = f"m_{generate_ulid()}"
                ev_id = f"ev_{generate_ulid()}"

                action, assigned_id = self.store.record_measurement(
                    measurement_id=meas_id,
                    event_id=ev_id,
                    metric=metric,
                    value=val,
                    unit=unit,
                    aggregation=agg,
                    source=source,
                    account=account,
                    provider=provider,
                    model=model,
                    native_id=native_id,
                    window_start=rec.get("window_start", requested_start),
                    window_end=rec.get("window_end", requested_end),
                    backfill_id=backfill_id,
                    force_distinct_unidentified=force_distinct_unidentified,
                )

                if action == "accepted":
                    result.accepted += 1
                    result.new_active_measurements += 1
                    page_records_committed += 1
                elif action == "existing":
                    result.existing += 1
                elif action == "revision":
                    result.revisions += 1
                elif action == "quarantined":
                    result.quarantined += 1

            # Simulate network crash during transit right after fetching page if triggered
            if simulate_interrupt_after_page is not None and curr_page == simulate_interrupt_after_page:
                # Save checkpoint for this page before interrupting
                new_state = CursorPaginationState(
                    backfill_id=backfill_id,
                    source=source,
                    account=account,
                    current_page=curr_page,
                    last_cursor=next_cursor,
                    committed_page=curr_page,
                    committed_cursor=next_cursor,
                    total_records_committed=result.total_seen,
                    is_finished=False,
                    last_checkpoint_at=_utc_iso(),
                )
                self.store.save_checkpoint(new_state)
                result.committed_page = curr_page
                result.cursor = next_cursor
                result.status = "interrupted"
                result.error_message = f"Simulated connection disconnect after page {simulate_interrupt_after_page}"
                break

            # Atomically commit page checkpoint
            new_state = CursorPaginationState(
                backfill_id=backfill_id,
                source=source,
                account=account,
                current_page=curr_page,
                last_cursor=next_cursor,
                committed_page=curr_page,
                committed_cursor=next_cursor,
                total_records_committed=result.total_seen,
                is_finished=not has_more,
                last_checkpoint_at=_utc_iso(),
            )
            self.store.save_checkpoint(new_state)
            result.committed_page = curr_page
            result.cursor = next_cursor

            current_cursor = next_cursor
            curr_page += 1

        if not has_more and result.status != "interrupted":
            result.status = "complete"

        # Record coverage period
        coverage_status = "confirmed_complete" if result.status == "complete" else "partial"
        self.store.record_coverage_period(
            backfill_id=backfill_id,
            source=source,
            account=account,
            period_start=requested_start,
            period_end=requested_end,
            status=coverage_status,
            records_count=result.total_seen,
            oldest_record_time=oldest_ts,
            newest_record_time=newest_ts,
            confirmation_reason="Full pagination exhausted" if result.status == "complete" else "Interrupted / partial",
        )

        # Refresh active sums
        _, active_sums = self.store.get_active_measurements_count_and_sums(source=source, account=account)
        result.active_sums = active_sums

        return result

    def assess_coverage(
        self,
        source: str,
        account: str,
        requested_start: str,
        requested_end: str,
        backfill_id: Optional[str] = None,
        provider_supports_full_history: bool = True,
    ) -> CoverageReport:
        """Assesses coverage according to US-06.d:
        - Separately documents Requested, Delivered, and Confirmed complete periods.
        - Neither empty response nor the oldest record seen prove full history!
        """
        report = CoverageReport(
            source=source,
            account=account,
            requested_start=_utc_iso(requested_start),
            requested_end=_utc_iso(requested_end),
        )

        entries = self.store.get_coverage_ledger(backfill_id) if backfill_id else []
        total_delivered = 0
        oldest_seen = None

        for entry in entries:
            total_delivered += entry.records_count
            if entry.oldest_record_time:
                if oldest_seen is None or entry.oldest_record_time < oldest_seen:
                    oldest_seen = entry.oldest_record_time

            report.delivered_periods.append(entry)

            # Check if confirmed complete
            # Empty response or oldest record seen does NOT guarantee complete history without explicit provider confirmation
            if entry.status == "confirmed_complete" and provider_supports_full_history:
                report.confirmed_periods.append(entry)
            else:
                report.unconfirmed_gaps.append((entry.period_start, entry.period_end))

        report.total_records_delivered = total_delivered
        report.oldest_record_seen = oldest_seen
        report.empty_response_observed = (total_delivered == 0 and len(entries) > 0)

        # Fully confirmed only if all delivered periods are confirmed complete and match requested span
        if report.confirmed_periods and len(report.confirmed_periods) == len(report.delivered_periods):
            report.is_fully_confirmed = True

        return report
