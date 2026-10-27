"""
storage/revisions module for AI Engineering Ledger (AEL).
Revision chains, corrections, auditability, supersedes linking, and history preservation.
Pure Python 3 standard library / sqlite3.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

ULID_PATTERN = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
EVIDENCE_ID_PATTERN = re.compile(r"^ev_[0-9A-HJKMNP-TV-Z]{26}$")
ALIAS_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SEMVER_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
TIMESTAMP_PATTERN = re.compile(
    r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T"
    r"([01][0-9]|2[0-3]):[0-5][0-9]:([0-5][0-9]|60)(\.[0-9]{1,9})?(Z|[+-]([01][0-9]|2[0-3]):[0-5][0-9])$"
)

CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

ALLOWED_REASON_CODES = {"initial", "reading_error", "provider_correction", "renormalization"}
ALLOWED_PROVENANCE_CLASSES = {"P0", "P1", "P2", "P3", "P4", "P5"}
ALLOWED_PROVENANCE_METHODS = {"api", "cli", "import", "otlp", "local", "manual", "derived", "estimate"}
ALLOWED_VALIDATION_STATUSES = {"unvalidated", "format_checked", "semantically_checked", "conflicted", "invalid"}

ALLOWED_UNITS = {"request", "token", "credit", "percent", "usd", "eur", "cny"}
ALLOWED_AGGREGATIONS = {"absolute", "delta", "interval_total", "gauge"}

ALLOWED_METRICS = {
    "requests",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cache_hit_tokens",
    "cache_miss_tokens",
    "total_tokens",
    "cost",
    "credit_balance",
    "credits_consumed",
    "subscription_price",
    "quota_used",
    "quota_remaining",
    "quota_limit",
    "quota_percent_used",
}

PERCENT_METRICS = {"quota_percent_used"}

NON_NEGATIVE_METRICS = {
    "requests",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cache_hit_tokens",
    "cache_miss_tokens",
    "total_tokens",
    "quota_used",
    "quota_remaining",
    "quota_limit",
}


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
        chars.append(CROCKFORD_ALPHABET[val & 0x1F])
        val >>= 5
    return "".join(reversed(chars))


def utc_now_iso() -> str:
    """Return current timestamp formatted as RFC 3339 UTC."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc_timestamp(ts: str) -> datetime:
    """Parse RFC 3339 timestamp with mandatory offset or Z into UTC datetime."""
    if not isinstance(ts, str) or not TIMESTAMP_PATTERN.match(ts):
        raise ValidationError(f"Invalid RFC 3339 timestamp with required offset: {ts}")
    s = ts.replace("Z", "+00:00")
    if "." in s:
        a, b = s.split(".", 1)
        i = 0
        while i < len(b) and b[i].isdigit():
            i += 1
        s = a + "." + b[:i][:6].ljust(6, "0") + b[i:]
    return datetime.fromisoformat(s).astimezone(timezone.utc)


class ValidationError(ValueError):
    """Raised when revision or measurement validation fails."""
    def __init__(self, message: str, field_name: Optional[str] = None):
        super().__init__(message)
        self.field_name = field_name


class RevisionCycleError(ValidationError):
    """Raised when a cycle is detected in a supersedes revision chain."""
    pass


class DanglingRevisionError(ValidationError):
    """Raised when supersedes references a non-existent revision."""
    pass


class ImmutableHistoryError(Exception):
    """Raised when attempting to overwrite or delete historical measurements or revisions directly."""
    pass


@dataclass
class LineageInfo:
    mapping_version: str
    transformation_version: str
    input_measurement_ids: List[str] = field(default_factory=list)

    def validate(self) -> None:
        if not isinstance(self.mapping_version, str) or not SEMVER_PATTERN.match(self.mapping_version):
            raise ValidationError(f"Invalid lineage mapping_version: {self.mapping_version}", "lineage.mapping_version")
        if not isinstance(self.transformation_version, str) or not SEMVER_PATTERN.match(self.transformation_version):
            raise ValidationError(f"Invalid lineage transformation_version: {self.transformation_version}", "lineage.transformation_version")
        if not isinstance(self.input_measurement_ids, list):
            raise ValidationError("lineage input_measurement_ids must be a list", "lineage.input_measurement_ids")
        for mid in self.input_measurement_ids:
            if not isinstance(mid, str) or not ULID_PATTERN.match(mid):
                raise ValidationError(f"Invalid ULID in input_measurement_ids: {mid}", "lineage.input_measurement_ids")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mapping_version": self.mapping_version,
            "transformation_version": self.transformation_version,
            "input_measurement_ids": list(self.input_measurement_ids),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> LineageInfo:
        return cls(
            mapping_version=d.get("mapping_version", ""),
            transformation_version=d.get("transformation_version", ""),
            input_measurement_ids=list(d.get("input_measurement_ids", [])),
        )


@dataclass
class RevisionRecord:
    revision_id: str
    revision_no: int
    supersedes: Optional[str]
    reason_code: str
    reason: str
    recorded_at: str
    effective: bool
    lineage: LineageInfo
    actor: Optional[str] = None

    def validate(self) -> None:
        if not isinstance(self.revision_id, str) or not ULID_PATTERN.match(self.revision_id):
            raise ValidationError(f"Invalid revision_id ULID: {self.revision_id}", "revision_id")
        if not isinstance(self.revision_no, int) or self.revision_no < 1:
            raise ValidationError(f"revision_no must be integer >= 1, got {self.revision_no}", "revision_no")
        if self.reason_code not in ALLOWED_REASON_CODES:
            raise ValidationError(f"Invalid reason_code: {self.reason_code}", "reason_code")
        if not isinstance(self.reason, str) or len(self.reason.strip()) == 0:
            raise ValidationError("reason must be a non-empty string", "reason")
        if not isinstance(self.recorded_at, str) or not TIMESTAMP_PATTERN.match(self.recorded_at):
            raise ValidationError(f"Invalid RFC 3339 recorded_at: {self.recorded_at}", "recorded_at")
        if not isinstance(self.effective, bool):
            raise ValidationError("effective must be boolean", "effective")
        if self.actor is not None and not isinstance(self.actor, str):
            raise ValidationError("actor must be a string", "actor")

        if self.revision_no == 1:
            if self.supersedes is not None:
                raise ValidationError("revision_no=1 must have supersedes=null", "supersedes")
            if self.reason_code != "initial":
                raise ValidationError("revision_no=1 must have reason_code='initial'", "reason_code")
        else:
            if self.supersedes is None or not isinstance(self.supersedes, str) or not ULID_PATTERN.match(self.supersedes):
                raise ValidationError("revision_no > 1 must have valid supersedes ULID", "supersedes")
            if self.reason_code == "initial":
                raise ValidationError("revision_no > 1 cannot have reason_code='initial'", "reason_code")

        self.lineage.validate()

    def to_dict(self) -> Dict[str, Any]:
        res = {
            "revision_id": self.revision_id,
            "revision_no": self.revision_no,
            "supersedes": self.supersedes,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "recorded_at": self.recorded_at,
            "effective": self.effective,
            "lineage": self.lineage.to_dict(),
        }
        if self.actor is not None:
            res["actor"] = self.actor
        return res

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> RevisionRecord:
        lineage_dict = d.get("lineage") or {}
        lineage = LineageInfo.from_dict(lineage_dict)
        return cls(
            revision_id=d["revision_id"],
            revision_no=d["revision_no"],
            supersedes=d.get("supersedes"),
            reason_code=d["reason_code"],
            reason=d["reason"],
            recorded_at=d["recorded_at"],
            effective=bool(d["effective"]),
            lineage=lineage,
            actor=d.get("actor"),
        )


@dataclass
class MeasurementRevisionRecord:
    """A measurement at a specific revision level."""
    measurement_id: str
    event_id: str
    metric: str
    value: Union[int, str]
    unit: str
    aggregation: str
    source: str
    source_account_id: str
    provenance: Dict[str, Any]
    evidence_ids: List[str]
    collector_version: str
    mapping_version: str
    revision: RevisionRecord
    provider: Optional[str] = None
    model: Optional[str] = None
    window: Optional[Dict[str, Any]] = None
    dimensions: Optional[Dict[str, Any]] = None
    schema_version: str = "0.1"

    def validate(self) -> None:
        if self.schema_version != "0.1":
            raise ValidationError(f"Invalid schema_version: {self.schema_version}", "schema_version")
        if not isinstance(self.measurement_id, str) or not ULID_PATTERN.match(self.measurement_id):
            raise ValidationError(f"Invalid measurement_id ULID: {self.measurement_id}", "measurement_id")
        if not isinstance(self.event_id, str) or not ULID_PATTERN.match(self.event_id):
            raise ValidationError(f"Invalid event_id ULID: {self.event_id}", "event_id")
        if self.metric not in ALLOWED_METRICS:
            raise ValidationError(f"Invalid metric: {self.metric}", "metric")
        if self.unit not in ALLOWED_UNITS:
            raise ValidationError(f"Invalid unit: {self.unit}", "unit")
        if self.aggregation not in ALLOWED_AGGREGATIONS:
            raise ValidationError(f"Invalid aggregation: {self.aggregation}", "aggregation")
        if not isinstance(self.source, str) or not ALIAS_PATTERN.match(self.source):
            raise ValidationError(f"Invalid source alias: {self.source}", "source")
        if not isinstance(self.source_account_id, str) or not ALIAS_PATTERN.match(self.source_account_id):
            raise ValidationError(f"Invalid source_account_id alias: {self.source_account_id}", "source_account_id")

        # Value validation: int or exact decimal string, no floats/NaN/Infinity
        if isinstance(self.value, float):
            raise ValidationError("float value forbidden (use integer or decimal string)", "value")
        if isinstance(self.value, bool):
            raise ValidationError("boolean value not allowed as measurement value", "value")
        try:
            d = Decimal(str(self.value))
        except (InvalidOperation, ValueError):
            raise ValidationError(f"Unparseable measurement value: {self.value}", "value")
        if not d.is_finite():
            raise ValidationError("non-finite value forbidden", "value")

        if d.is_zero():
            d = Decimal(0)

        if self.metric in NON_NEGATIVE_METRICS and d < 0:
            raise ValidationError(f"negative value forbidden for metric {self.metric}: {self.value}", "value")

        if self.metric in PERCENT_METRICS:
            if d < 0 or d > 100:
                raise ValidationError(f"percentage metric {self.metric} out of range [0, 100]: {self.value}", "value")

        if self.unit in ("token", "request") and d != d.to_integral_value():
            raise ValidationError(f"fractional token/request count forbidden: {self.value}", "value")

        # Evidence bindings: must have minItems 1
        if not isinstance(self.evidence_ids, list) or len(self.evidence_ids) == 0:
            raise ValidationError("evidence_ids must be a non-empty list", "evidence_ids")
        for eid in self.evidence_ids:
            if not isinstance(eid, str) or not EVIDENCE_ID_PATTERN.match(eid):
                raise ValidationError(f"Invalid evidence_id format: {eid}", "evidence_ids")

        # Provenance validation
        if not isinstance(self.provenance, dict):
            raise ValidationError("provenance must be a dict", "provenance")
        p_class = self.provenance.get("class")
        p_method = self.provenance.get("method")
        p_status = self.provenance.get("validation_status")
        p_conf = self.provenance.get("confidence")
        p_reason = self.provenance.get("confidence_reason")
        if p_class not in ALLOWED_PROVENANCE_CLASSES:
            raise ValidationError(f"Invalid provenance class: {p_class}", "provenance.class")
        if p_method not in ALLOWED_PROVENANCE_METHODS:
            raise ValidationError(f"Invalid provenance method: {p_method}", "provenance.method")
        if p_status not in ALLOWED_VALIDATION_STATUSES:
            raise ValidationError(f"Invalid provenance validation_status: {p_status}", "provenance.validation_status")
        if not isinstance(p_conf, (int, float)) or p_conf < 0 or p_conf > 1:
            raise ValidationError("provenance confidence must be number in [0, 1]", "provenance.confidence")
        if not isinstance(p_reason, str) or len(p_reason.strip()) == 0:
            raise ValidationError("provenance confidence_reason must be non-empty", "provenance.confidence_reason")

        if not isinstance(self.collector_version, str) or len(self.collector_version.strip()) == 0:
            raise ValidationError("collector_version must be non-empty string", "collector_version")
        if not isinstance(self.mapping_version, str) or not SEMVER_PATTERN.match(self.mapping_version):
            raise ValidationError(f"Invalid mapping_version semver: {self.mapping_version}", "mapping_version")

        # Validate nested revision
        self.revision.validate()

    def to_dict(self) -> Dict[str, Any]:
        res = {
            "schema_version": self.schema_version,
            "measurement_id": self.measurement_id,
            "event_id": self.event_id,
            "metric": self.metric,
            "value": self.value,
            "unit": self.unit,
            "aggregation": self.aggregation,
            "source": self.source,
            "source_account_id": self.source_account_id,
            "provenance": dict(self.provenance),
            "evidence_ids": list(self.evidence_ids),
            "collector_version": self.collector_version,
            "mapping_version": self.mapping_version,
            "revision": self.revision.to_dict(),
        }
        if self.provider is not None:
            res["provider"] = self.provider
        if self.model is not None:
            res["model"] = self.model
        if self.window is not None:
            res["window"] = dict(self.window)
        if self.dimensions is not None:
            res["dimensions"] = dict(self.dimensions)
        return res

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> MeasurementRevisionRecord:
        rev_dict = d["revision"]
        rev = RevisionRecord.from_dict(rev_dict)
        return cls(
            schema_version=d.get("schema_version", "0.1"),
            measurement_id=d["measurement_id"],
            event_id=d["event_id"],
            metric=d["metric"],
            value=d["value"],
            unit=d["unit"],
            aggregation=d["aggregation"],
            source=d["source"],
            source_account_id=d["source_account_id"],
            provenance=d["provenance"],
            evidence_ids=list(d["evidence_ids"]),
            collector_version=d["collector_version"],
            mapping_version=d["mapping_version"],
            revision=rev,
            provider=d.get("provider"),
            model=d.get("model"),
            window=d.get("window"),
            dimensions=d.get("dimensions"),
        )


def check_revision_chain(revs: Sequence[RevisionRecord]) -> List[str]:
    """
    Validates a list of revisions belonging to ONE logical measurement chain.
    Enforces:
    - Unique revision_ids
    - Acyclic supersedes links
    - No dangling supersedes links (each pointer must resolve within the chain)
    - Exactly one effective revision
    - The effective revision must not be superseded by another revision
    """
    errors: List[str] = []
    by_id: Dict[str, RevisionRecord] = {}
    for r in revs:
        if r.revision_id in by_id:
            errors.append(f"duplicate revision_id {r.revision_id}")
        by_id[r.revision_id] = r

    for r in revs:
        s = r.supersedes
        seen = {r.revision_id}
        while s is not None:
            if s in seen:
                errors.append(f"cycle at {r.revision_id}")
                break
            seen.add(s)
            if s not in by_id:
                errors.append(f"dangling supersedes {s}")
                break
            s = by_id[s].supersedes

    eff = [r for r in revs if r.effective]
    if len(eff) != 1:
        errors.append(f"exactly one effective revision required, got {len(eff)}")
    else:
        effective_id = eff[0].revision_id
        if any(r.supersedes == effective_id for r in revs):
            errors.append(f"effective revision {effective_id} is superseded")

    return errors


class RevisionStore:
    """
    Persistent store for measurement revisions, lineage, and audit trails.
    History preservation:
    - Never overwrites or deletes historical revisions or measurements.
    - Corrections produce new revisions with explicit supersedes, reason, actor, and evidence.
    - Re-normalization creates a traceable new revision activating the new state without
      modifying original evidence or previous derived states.
    - Default reports count ONLY the effective revision.
    - 'As-of' / 'prior-state' views faithfully reproduce historical states.
    """

    def __init__(self, db_path: Union[str, Path] = ":memory:"):
        self.db_path = str(db_path)
        self._shared_conn: Optional[sqlite3.Connection] = None
        if self.db_path == ":memory:":
            self._shared_conn = sqlite3.connect(":memory:")
            self._shared_conn.row_factory = sqlite3.Row
            self._shared_conn.execute("PRAGMA foreign_keys = ON")
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        if self._shared_conn is not None:
            return self._shared_conn
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def close(self) -> None:
        if self._shared_conn is not None:
            self._shared_conn.close()
            self._shared_conn = None

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            # Evidence catalog table (known evidence descriptors for binding validation)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS evidence_descriptors (
                    evidence_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    source_account_id TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    registered_at TEXT NOT NULL,
                    raw_data BLOB
                )
                """
            )

            # Measurement revisions table: stores all revisions immutably
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS measurement_revisions (
                    revision_id TEXT PRIMARY KEY,
                    measurement_id TEXT NOT NULL,
                    revision_no INTEGER NOT NULL,
                    supersedes TEXT,
                    reason_code TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    actor TEXT,
                    recorded_at TEXT NOT NULL,
                    effective INTEGER NOT NULL,
                    metric TEXT NOT NULL,
                    value_str TEXT NOT NULL,
                    unit TEXT NOT NULL,
                    aggregation TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_account_id TEXT NOT NULL,
                    mapping_version TEXT NOT NULL,
                    transformation_version TEXT NOT NULL,
                    collector_version TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    evidence_ids_json TEXT NOT NULL,
                    provenance_json TEXT NOT NULL,
                    lineage_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY (supersedes) REFERENCES measurement_revisions(revision_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_meas_rev_mid ON measurement_revisions(measurement_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_meas_rev_eff ON measurement_revisions(measurement_id, effective)"
            )

            # Immutable audit log recording every mutation attempt and revision transition
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS revision_audit_log (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    measurement_id TEXT NOT NULL,
                    revision_id TEXT NOT NULL,
                    actor TEXT,
                    reason_code TEXT,
                    reason TEXT,
                    timestamp TEXT NOT NULL,
                    details_json TEXT NOT NULL
                )
                """
            )

    def register_evidence(
        self,
        evidence_id: str,
        source: str,
        source_account_id: str,
        raw_bytes: bytes,
        sha256: Optional[str] = None,
    ) -> None:
        """Register an evidence descriptor and bytes for binding validation."""
        actual_sha = hashlib.sha256(raw_bytes).hexdigest()
        if sha256 is not None and sha256 != actual_sha:
            raise ValidationError(f"Evidence SHA256 mismatch: {sha256} != {actual_sha}", "sha256")
        size_bytes = len(raw_bytes)
        now = utc_now_iso()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO evidence_descriptors
                (evidence_id, source, source_account_id, sha256, size_bytes, registered_at, raw_data)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (evidence_id, source, source_account_id, actual_sha, size_bytes, now, raw_bytes),
            )

    def record_initial_measurement(
        self,
        measurement: MeasurementRevisionRecord,
        actor: Optional[str] = "system",
    ) -> MeasurementRevisionRecord:
        """
        Record a brand new measurement (revision_no = 1, supersedes = null, reason_code = 'initial').
        Strictly preserves history: fails if measurement_id already exists.
        """
        if measurement.revision.revision_no != 1:
            raise ValidationError("Initial measurement must have revision_no=1", "revision.revision_no")
        if measurement.revision.supersedes is not None:
            raise ValidationError("Initial measurement cannot supersede another revision", "revision.supersedes")
        if measurement.revision.reason_code != "initial":
            raise ValidationError("Initial measurement must have reason_code='initial'", "revision.reason_code")
        if not measurement.revision.effective:
            raise ValidationError("Initial measurement must be marked effective", "revision.effective")

        measurement.validate()

        # Validate evidence bindings against known descriptors
        self._validate_evidence_bindings(measurement.evidence_ids, measurement.source, measurement.source_account_id)

        with self._get_connection() as conn:
            existing = conn.execute(
                "SELECT COUNT(*) FROM measurement_revisions WHERE measurement_id = ?",
                (measurement.measurement_id,),
            ).fetchone()[0]
            if existing > 0:
                raise ImmutableHistoryError(
                    f"Measurement {measurement.measurement_id} already exists! Overwriting is forbidden. "
                    "Use correct_measurement() to create a new revision."
                )

            self._insert_revision(conn, measurement, actor)

        return measurement

    def correct_measurement(
        self,
        measurement_id: str,
        new_value: Union[int, str],
        reason_code: str,
        reason: str,
        actor: str,
        evidence_ids: Optional[List[str]] = None,
        new_metric: Optional[str] = None,
        new_unit: Optional[str] = None,
        new_provenance: Optional[Dict[str, Any]] = None,
        recorded_at: Optional[str] = None,
    ) -> MeasurementRevisionRecord:
        """
        US-04.a: Apply a correction to an existing measurement.
        Enforces:
        - History is preserved: previous revision is untouched and preserved in storage.
        - Exactly 1 effective revision exists after the operation.
        - Previous effective revision becomes effective=False and is superseded.
        - New revision points to the previous revision via `supersedes`.
        - Cycles and dangling links are validated and rejected.
        - Reason, actor, and evidence binding are strictly enforced.
        """
        if reason_code not in ("reading_error", "provider_correction"):
            raise ValidationError(
                f"Correction requires reason_code 'reading_error' or 'provider_correction', got: {reason_code}",
                "reason_code",
            )
        if not reason or not isinstance(reason, str) or len(reason.strip()) == 0:
            raise ValidationError("Correction requires a non-empty reason", "reason")
        if not actor or not isinstance(actor, str) or len(actor.strip()) == 0:
            raise ValidationError("Correction requires an explicit actor", "actor")

        with self._get_connection() as conn:
            # Fetch current effective revision
            rows = conn.execute(
                "SELECT * FROM measurement_revisions WHERE measurement_id = ? ORDER BY revision_no ASC",
                (measurement_id,),
            ).fetchall()
            if not rows:
                raise ValidationError(f"Measurement {measurement_id} not found", "measurement_id")

            chain_records = [self._row_to_record(r) for r in rows]
            current_effective = [r for r in chain_records if r.revision.effective]
            if len(current_effective) != 1:
                raise ValidationError(
                    f"Inconsistent state: expected 1 effective revision for {measurement_id}, found {len(current_effective)}"
                )

            parent = current_effective[0]
            new_rev_no = parent.revision.revision_no + 1
            new_rev_id = generate_ulid()
            now_iso = recorded_at or utc_now_iso()

            target_evidence_ids = evidence_ids if evidence_ids is not None else list(parent.evidence_ids)
            self._validate_evidence_bindings(target_evidence_ids, parent.source, parent.source_account_id)

            new_revision = RevisionRecord(
                revision_id=new_rev_id,
                revision_no=new_rev_no,
                supersedes=parent.revision.revision_id,
                reason_code=reason_code,
                reason=reason,
                recorded_at=now_iso,
                effective=True,
                lineage=LineageInfo(
                    mapping_version=parent.mapping_version,
                    transformation_version=parent.revision.lineage.transformation_version,
                    input_measurement_ids=list(parent.revision.lineage.input_measurement_ids),
                ),
                actor=actor,
            )

            corrected_record = MeasurementRevisionRecord(
                schema_version="0.1",
                measurement_id=measurement_id,
                event_id=parent.event_id,
                metric=new_metric or parent.metric,
                value=new_value,
                unit=new_unit or parent.unit,
                aggregation=parent.aggregation,
                source=parent.source,
                source_account_id=parent.source_account_id,
                provenance=new_provenance or dict(parent.provenance),
                evidence_ids=target_evidence_ids,
                collector_version=parent.collector_version,
                mapping_version=parent.mapping_version,
                revision=new_revision,
                provider=parent.provider,
                model=parent.model,
                window=dict(parent.window) if parent.window else None,
                dimensions=dict(parent.dimensions) if parent.dimensions else None,
            )
            corrected_record.validate()

            # Pre-validate hypothetical chain to prevent any cycle or consistency error
            simulated_chain = [
                RevisionRecord.from_dict({**r.revision.to_dict(), "effective": False})
                if r.revision.revision_id == parent.revision.revision_id
                else r.revision
                for r in chain_records
            ]
            simulated_chain.append(new_revision)
            errors = check_revision_chain(simulated_chain)
            if errors:
                if any("cycle" in e for e in errors):
                    raise RevisionCycleError(f"Revision cycle detected: {errors}")
                if any("dangling" in e for e in errors):
                    raise DanglingRevisionError(f"Dangling revision link: {errors}")
                raise ValidationError(f"Revision chain validation failed: {errors}")

            # Transactionally mark previous effective revision as non-effective, and insert new revision
            conn.execute(
                "UPDATE measurement_revisions SET effective = 0 WHERE revision_id = ?",
                (parent.revision.revision_id,),
            )
            self._insert_revision(conn, corrected_record, actor)

            # Audit log
            conn.execute(
                """
                INSERT INTO revision_audit_log
                (action, measurement_id, revision_id, actor, reason_code, reason, timestamp, details_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "correction",
                    measurement_id,
                    new_rev_id,
                    actor,
                    reason_code,
                    reason,
                    now_iso,
                    json.dumps({
                        "supersedes": parent.revision.revision_id,
                        "old_value": parent.value,
                        "new_value": new_value,
                    }),
                ),
            )

        return corrected_record

    def renormalize_measurement(
        self,
        measurement_id: str,
        new_mapping_version: str,
        new_value: Union[int, str],
        reason: str,
        actor: str,
        new_unit: Optional[str] = None,
        transformation_version: Optional[str] = None,
        recorded_at: Optional[str] = None,
    ) -> MeasurementRevisionRecord:
        """
        US-04.c: Re-normalization with a new mapping version.
        Overwrites NEITHER the original evidence nor the previous derived state.
        Activates a traceable new state with reason_code='renormalization'.
        """
        if not isinstance(new_mapping_version, str) or not SEMVER_PATTERN.match(new_mapping_version):
            raise ValidationError(f"Invalid new_mapping_version semver: {new_mapping_version}", "mapping_version")
        if not reason or not isinstance(reason, str) or len(reason.strip()) == 0:
            raise ValidationError("Renormalization requires a non-empty reason", "reason")
        if not actor or not isinstance(actor, str) or len(actor.strip()) == 0:
            raise ValidationError("Renormalization requires an explicit actor", "actor")

        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM measurement_revisions WHERE measurement_id = ? ORDER BY revision_no ASC",
                (measurement_id,),
            ).fetchall()
            if not rows:
                raise ValidationError(f"Measurement {measurement_id} not found", "measurement_id")

            chain_records = [self._row_to_record(r) for r in rows]
            current_effective = [r for r in chain_records if r.revision.effective]
            if len(current_effective) != 1:
                raise ValidationError(f"Expected 1 effective revision for {measurement_id}")

            parent = current_effective[0]
            new_rev_no = parent.revision.revision_no + 1
            new_rev_id = generate_ulid()
            now_iso = recorded_at or utc_now_iso()

            target_trans_version = transformation_version or parent.revision.lineage.transformation_version

            new_revision = RevisionRecord(
                revision_id=new_rev_id,
                revision_no=new_rev_no,
                supersedes=parent.revision.revision_id,
                reason_code="renormalization",
                reason=reason,
                recorded_at=now_iso,
                effective=True,
                lineage=LineageInfo(
                    mapping_version=new_mapping_version,
                    transformation_version=target_trans_version,
                    input_measurement_ids=list(parent.revision.lineage.input_measurement_ids),
                ),
                actor=actor,
            )

            renorm_record = MeasurementRevisionRecord(
                schema_version="0.1",
                measurement_id=measurement_id,
                event_id=parent.event_id,
                metric=parent.metric,
                value=new_value,
                unit=new_unit or parent.unit,
                aggregation=parent.aggregation,
                source=parent.source,
                source_account_id=parent.source_account_id,
                provenance=dict(parent.provenance),
                evidence_ids=list(parent.evidence_ids),  # raw evidence remains unchanged!
                collector_version=parent.collector_version,
                mapping_version=new_mapping_version,
                revision=new_revision,
                provider=parent.provider,
                model=parent.model,
                window=dict(parent.window) if parent.window else None,
                dimensions=dict(parent.dimensions) if parent.dimensions else None,
            )
            renorm_record.validate()

            simulated_chain = [
                RevisionRecord.from_dict({**r.revision.to_dict(), "effective": False})
                if r.revision.revision_id == parent.revision.revision_id
                else r.revision
                for r in chain_records
            ]
            simulated_chain.append(new_revision)
            errors = check_revision_chain(simulated_chain)
            if errors:
                raise ValidationError(f"Revision chain validation failed: {errors}")

            conn.execute(
                "UPDATE measurement_revisions SET effective = 0 WHERE revision_id = ?",
                (parent.revision.revision_id,),
            )
            self._insert_revision(conn, renorm_record, actor)

            conn.execute(
                """
                INSERT INTO revision_audit_log
                (action, measurement_id, revision_id, actor, reason_code, reason, timestamp, details_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "renormalization",
                    measurement_id,
                    new_rev_id,
                    actor,
                    "renormalization",
                    reason,
                    now_iso,
                    json.dumps({
                        "supersedes": parent.revision.revision_id,
                        "old_mapping_version": parent.mapping_version,
                        "new_mapping_version": new_mapping_version,
                        "old_value": parent.value,
                        "new_value": new_value,
                    }),
                ),
            )

        return renorm_record

    def get_effective_measurement(self, measurement_id: str) -> Optional[MeasurementRevisionRecord]:
        """Fetch the currently active/effective revision of a measurement."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM measurement_revisions WHERE measurement_id = ? AND effective = 1",
                (measurement_id,),
            ).fetchone()
            if not row:
                return None
            return self._row_to_record(row)

    def get_measurement_prior_to_revision(
        self,
        measurement_id: str,
        revision_id: str,
    ) -> Optional[MeasurementRevisionRecord]:
        """
        US-04.b: Ansicht „Datenstand vor Korrektur“ reproduziert exakt den vorherigen Wert.
        Resolves the exact measurement record superseded by `revision_id`.
        """
        with self._get_connection() as conn:
            target_row = conn.execute(
                "SELECT supersedes FROM measurement_revisions WHERE measurement_id = ? AND revision_id = ?",
                (measurement_id, revision_id),
            ).fetchone()
            if not target_row:
                raise ValidationError(f"Revision {revision_id} for measurement {measurement_id} not found")
            superseded_id = target_row["supersedes"]
            if superseded_id is None:
                return None  # revision_no=1 has no prior state

            prior_row = conn.execute(
                "SELECT * FROM measurement_revisions WHERE revision_id = ?",
                (superseded_id,),
            ).fetchone()
            if not prior_row:
                raise DanglingRevisionError(f"Prior revision {superseded_id} could not be resolved")
            return self._row_to_record(prior_row)

    def get_measurement_as_of(
        self,
        measurement_id: str,
        as_of_timestamp: str,
    ) -> Optional[MeasurementRevisionRecord]:
        """
        US-04.b: Point-in-time time-travel view: reproduce the value active at `as_of_timestamp`.
        Selects the latest revision recorded_at <= as_of_timestamp.
        """
        target_utc = parse_utc_timestamp(as_of_timestamp)
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM measurement_revisions WHERE measurement_id = ? ORDER BY revision_no ASC",
                (measurement_id,),
            ).fetchall()
            if not rows:
                return None

            candidates = []
            for r in rows:
                r_utc = parse_utc_timestamp(r["recorded_at"])
                if r_utc <= target_utc:
                    candidates.append(r)

            if not candidates:
                return None  # measurement did not exist yet at as_of_timestamp

            # Latest recorded revision prior to or at as_of_timestamp
            chosen = candidates[-1]
            return self._row_to_record(chosen)

    def get_revision_history(self, measurement_id: str) -> List[MeasurementRevisionRecord]:
        """Return full immutable history of revisions for a measurement, ordered by revision_no."""
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM measurement_revisions WHERE measurement_id = ? ORDER BY revision_no ASC",
                (measurement_id,),
            ).fetchall()
            return [self._row_to_record(r) for r in rows]

    def validate_measurement_chain(self, measurement_id: str) -> List[str]:
        """Validate chain consistency, cyclicity, and dangling pointers for a measurement."""
        history = self.get_revision_history(measurement_id)
        if not history:
            return ["measurement not found"]
        return check_revision_chain([m.revision for m in history])

    def generate_standard_report(
        self,
        source: Optional[str] = None,
        source_account_id: Optional[str] = None,
        metric: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        US-04.a: Standardberichte zählen ausschließlich die wirksame Revision (`effective = 1`).
        Superseded, obsolete revisions are excluded from counts and aggregations.
        """
        query = "SELECT * FROM measurement_revisions WHERE effective = 1"
        params: List[Any] = []
        if source:
            query += " AND source = ?"
            params.append(source)
        if source_account_id:
            query += " AND source_account_id = ?"
            params.append(source_account_id)
        if metric:
            query += " AND metric = ?"
            params.append(metric)

        with self._get_connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            records = [self._row_to_record(r) for r in rows]

            # Also check total revisions in existence to prove filtering
            total_revs_count = conn.execute("SELECT COUNT(*) FROM measurement_revisions").fetchone()[0]

        # Aggregate totals by metric and unit
        totals_by_metric: Dict[str, Dict[str, Decimal]] = {}
        for rec in records:
            d_val = Decimal(str(rec.value))
            m = rec.metric
            u = rec.unit
            if m not in totals_by_metric:
                totals_by_metric[m] = {}
            totals_by_metric[m][u] = totals_by_metric[m].get(u, Decimal(0)) + d_val

        formatted_totals = {
            m: {u: str(val) for u, val in units_dict.items()}
            for m, units_dict in totals_by_metric.items()
        }

        return {
            "report_timestamp": utc_now_iso(),
            "scope": {
                "source": source,
                "source_account_id": source_account_id,
                "metric": metric,
            },
            "total_effective_measurements": len(records),
            "total_revisions_stored": total_revs_count,
            "superseded_revisions_excluded": total_revs_count - len(records),
            "totals": formatted_totals,
            "measurements": [r.to_dict() for r in records],
        }

    def attempt_direct_overwrite(self, measurement_id: str, fake_value: Any) -> None:
        """Explicit probe method: attempting to overwrite history directly is strictly forbidden."""
        raise ImmutableHistoryError(
            f"Overwriting historical measurement {measurement_id} is forbidden! "
            "Ledger history is append-only; use correct_measurement()."
        )

    def _validate_evidence_bindings(self, evidence_ids: List[str], source: str, source_account: str) -> None:
        """Validate that referenced evidence IDs exist and have matching source/account scope."""
        with self._get_connection() as conn:
            for eid in evidence_ids:
                row = conn.execute(
                    "SELECT source, source_account_id FROM evidence_descriptors WHERE evidence_id = ?",
                    (eid,),
                ).fetchone()
                if not row:
                    # If not registered in descriptors, we still verify format; if descriptor table has entries, we enforce binding
                    total_desc = conn.execute("SELECT COUNT(*) FROM evidence_descriptors").fetchone()[0]
                    if total_desc > 0:
                        raise ValidationError(f"Evidence {eid} not found in evidence descriptors", "evidence_ids")
                    continue
                if row["source"] != source or row["source_account_id"] != source_account:
                    raise ValidationError(
                        f"Evidence {eid} scope mismatch: registered ({row['source']}, {row['source_account_id']}) "
                        f"!= measurement ({source}, {source_account})",
                        "evidence_ids",
                    )

    def _insert_revision(self, conn: sqlite3.Connection, rec: MeasurementRevisionRecord, actor: Optional[str]) -> None:
        raw_dict = rec.to_dict()
        conn.execute(
            """
            INSERT INTO measurement_revisions (
                revision_id, measurement_id, revision_no, supersedes, reason_code, reason,
                actor, recorded_at, effective, metric, value_str, unit, aggregation,
                source, source_account_id, mapping_version, transformation_version,
                collector_version, event_id, evidence_ids_json, provenance_json,
                lineage_json, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec.revision.revision_id,
                rec.measurement_id,
                rec.revision.revision_no,
                rec.revision.supersedes,
                rec.revision.reason_code,
                rec.revision.reason,
                actor or rec.revision.actor,
                rec.revision.recorded_at,
                1 if rec.revision.effective else 0,
                rec.metric,
                str(rec.value),
                rec.unit,
                rec.aggregation,
                rec.source,
                rec.source_account_id,
                rec.mapping_version,
                rec.revision.lineage.transformation_version,
                rec.collector_version,
                rec.event_id,
                json.dumps(rec.evidence_ids),
                json.dumps(rec.provenance),
                json.dumps(rec.revision.lineage.to_dict()),
                json.dumps(raw_dict),
            ),
        )

    def _row_to_record(self, row: sqlite3.Row) -> MeasurementRevisionRecord:
        payload = json.loads(row["payload_json"])
        # Ensure row's current effective state overrides payload cache
        payload["revision"]["effective"] = bool(row["effective"])
        return MeasurementRevisionRecord.from_dict(payload)
