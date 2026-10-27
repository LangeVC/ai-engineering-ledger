"""
storage/evidence module for AI Engineering Ledger (AEL).
Immutable evidence & raw artifact storage, integrity verification, and redaction management.
Pure Python 3 standard library / sqlite3.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import tarfile
import time
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

# Regex patterns matching core schemas
ULID_PATTERN = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
EVIDENCE_ID_PATTERN = re.compile(r"^ev_[0-9A-HJKMNP-TV-Z]{26}$")
ALIAS_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
TIMESTAMP_PATTERN = re.compile(
    r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T"
    r"([01][0-9]|2[0-3]):[0-5][0-9]:([0-5][0-9]|60)(\.[0-9]{1,9})?(Z|[+-]([01][0-9]|2[0-3]):[0-5][0-9])$"
)
SEMVER_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")

CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

ALLOWED_PROVENANCE_CLASSES = {"P0", "P1", "P2", "P3", "P4", "P5"}
ALLOWED_PROVENANCE_METHODS = {"api", "cli", "import", "otlp", "local", "manual", "derived", "estimate"}
ALLOWED_VALIDATION_STATUSES = {"unvalidated", "format_checked", "semantically_checked", "conflicted", "invalid"}

ALLOWED_CAPTURE_POINTS = {
    "provider_response_verbatim",
    "post_redaction",
    "user_export_file",
    "user_redacted_screenshot",
    "local_log_extract",
}

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

# Cumulative / count metrics that must be non-negative
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

    # 48-bit timestamp
    ts_bytes = timestamp_ms.to_bytes(6, byteorder="big")
    # 80-bit randomness
    rand_bytes = os.urandom(10)
    data = ts_bytes + rand_bytes

    # Encode into 26 base32 characters (128 bits total: 5 bits * 26 = 130 bits, 2 MSB zero)
    val = int.from_bytes(data, byteorder="big")
    chars = []
    for _ in range(26):
        chars.append(CROCKFORD_ALPHABET[val & 0x1F])
        val >>= 5
    return "".join(reversed(chars))


def generate_evidence_id(timestamp_ms: Optional[int] = None) -> str:
    """Generate an evidence id: ev_<ulid>."""
    return f"ev_{generate_ulid(timestamp_ms)}"


def compute_sha256(data: bytes) -> str:
    """Compute hex sha256 of byte array."""
    return hashlib.sha256(data).hexdigest()


def utc_now_iso() -> str:
    """Return current timestamp formatted as RFC 3339 UTC."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ValidationError(ValueError):
    """Raised when measurement, evidence or payload validation fails."""
    def __init__(self, message: str, field_name: Optional[str] = None, line_number: Optional[int] = None):
        super().__init__(message)
        self.field_name = field_name
        self.line_number = line_number


class IntegrityError(Exception):
    """Raised when an artifact hash check fails or tampering is detected."""
    pass


class ImmutableStorageError(Exception):
    """Raised when modification of an existing immutable artifact is attempted."""
    pass


@dataclass
class RedactionInfo:
    redacted: bool
    redaction_version: Optional[str] = None
    redaction_reason: Optional[str] = None

    def validate(self, capture_point: str) -> None:
        if self.redacted:
            if not self.redaction_version or not isinstance(self.redaction_version, str) or not self.redaction_version.strip():
                raise ValidationError("Redacted evidence must specify redaction_version", "redaction.redaction_version")
            if capture_point == "provider_response_verbatim":
                raise ValidationError(
                    "Redacted evidence cannot have capture_point 'provider_response_verbatim'",
                    "capture_point"
                )
        else:
            if self.redaction_version is not None:
                raise ValidationError(
                    "Non-redacted evidence must have redaction_version=null",
                    "redaction.redaction_version"
                )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "redacted": self.redacted,
            "redaction_version": self.redaction_version,
            "redaction_reason": self.redaction_reason,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> RedactionInfo:
        if "original_sha256" in d:
            raise ValidationError(
                "Forbidden: original_sha256 cannot be stored as secrets must not be archived (PRD 10.3)",
                "redaction.original_sha256"
            )
        return cls(
            redacted=bool(d.get("redacted", False)),
            redaction_version=d.get("redaction_version"),
            redaction_reason=d.get("redaction_reason"),
        )


@dataclass
class EvidenceArtifact:
    evidence_id: str
    sha256: str
    size_bytes: int
    mime_type: str
    source: str
    account: str
    created_at: str
    collector_version: str
    capture_point: str
    redaction: RedactionInfo
    schema_version: str = "0.1"
    observed_at: Optional[str] = None
    stored_relpath: Optional[str] = None
    metadata_json: Optional[Dict[str, Any]] = None

    def validate(self) -> None:
        if self.schema_version != "0.1":
            raise ValidationError(f"Unsupported schema_version: {self.schema_version}", "schema_version")
        if not EVIDENCE_ID_PATTERN.match(self.evidence_id):
            raise ValidationError(f"Invalid evidence_id format: {self.evidence_id}", "evidence_id")
        if not SHA256_PATTERN.match(self.sha256):
            raise ValidationError(f"Invalid sha256 hash: {self.sha256}", "sha256")
        if not isinstance(self.size_bytes, int) or self.size_bytes < 0:
            raise ValidationError("size_bytes must be non-negative integer", "size_bytes")
        if not self.mime_type or not isinstance(self.mime_type, str):
            raise ValidationError("mime_type must be non-empty string", "mime_type")
        if not ALIAS_PATTERN.match(self.source):
            raise ValidationError(f"Invalid source alias: {self.source}", "source")
        if not ALIAS_PATTERN.match(self.account):
            raise ValidationError(f"Invalid account alias: {self.account}", "account")
        if not TIMESTAMP_PATTERN.match(self.created_at):
            raise ValidationError(f"Invalid created_at RFC3339 timestamp: {self.created_at}", "created_at")
        if self.observed_at and not TIMESTAMP_PATTERN.match(self.observed_at):
            raise ValidationError(f"Invalid observed_at RFC3339 timestamp: {self.observed_at}", "observed_at")
        if not self.collector_version or not isinstance(self.collector_version, str):
            raise ValidationError("collector_version must be non-empty", "collector_version")
        if self.capture_point not in ALLOWED_CAPTURE_POINTS:
            raise ValidationError(f"Invalid capture_point: {self.capture_point}", "capture_point")
        self.redaction.validate(self.capture_point)

    def to_descriptor(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "schema_version": self.schema_version,
            "evidence_id": self.evidence_id,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "mime_type": self.mime_type,
            "source": self.source,
            "account": self.account,
            "created_at": self.created_at,
            "collector_version": self.collector_version,
            "capture_point": self.capture_point,
            "redaction": {
                "redacted": self.redaction.redacted,
                "redaction_version": self.redaction.redaction_version,
            },
        }
        if self.observed_at:
            d["observed_at"] = self.observed_at
        return d

    @classmethod
    def from_descriptor(cls, d: Dict[str, Any], stored_relpath: Optional[str] = None, metadata_json: Optional[Dict[str, Any]] = None) -> EvidenceArtifact:
        redaction = RedactionInfo.from_dict(d.get("redaction", {}))
        art = cls(
            schema_version=d.get("schema_version", "0.1"),
            evidence_id=d["evidence_id"],
            sha256=d["sha256"],
            size_bytes=d["size_bytes"],
            mime_type=d["mime_type"],
            source=d["source"],
            account=d["account"],
            created_at=d["created_at"],
            observed_at=d.get("observed_at"),
            collector_version=d["collector_version"],
            capture_point=d["capture_point"],
            redaction=redaction,
            stored_relpath=stored_relpath,
            metadata_json=metadata_json,
        )
        art.validate()
        return art


@dataclass
class Provenance:
    provenance_class: str
    method: str
    confidence: float
    confidence_reason: str
    validation_status: str = "unvalidated"
    reconciled_at: Optional[str] = None

    def validate(self) -> None:
        if self.provenance_class not in ALLOWED_PROVENANCE_CLASSES:
            raise ValidationError(f"Invalid provenance class: {self.provenance_class}", "provenance.class")
        if self.method not in ALLOWED_PROVENANCE_METHODS:
            raise ValidationError(f"Invalid provenance method: {self.method}", "provenance.method")
        if not isinstance(self.confidence, (int, float)) or not (0.0 <= self.confidence <= 1.0):
            raise ValidationError(f"Confidence must be between 0.0 and 1.0, got {self.confidence}", "provenance.confidence")
        if not self.confidence_reason or not isinstance(self.confidence_reason, str):
            raise ValidationError("confidence_reason must be non-empty string", "provenance.confidence_reason")
        if self.validation_status not in ALLOWED_VALIDATION_STATUSES:
            raise ValidationError(f"Invalid validation_status: {self.validation_status}", "provenance.validation_status")
        if self.reconciled_at and not TIMESTAMP_PATTERN.match(self.reconciled_at):
            raise ValidationError("Invalid reconciled_at timestamp", "provenance.reconciled_at")

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "class": self.provenance_class,
            "method": self.method,
            "confidence": self.confidence,
            "confidence_reason": self.confidence_reason,
            "validation_status": self.validation_status,
        }
        if self.reconciled_at:
            d["reconciled_at"] = self.reconciled_at
        return d


@dataclass
class Measurement:
    measurement_id: str
    event_id: str
    metric: str
    value: Union[int, Decimal, str]
    unit: str
    aggregation: str
    source: str
    source_account_id: str
    provenance: Provenance
    evidence_ids: List[str]
    collector_version: str
    mapping_version: str
    schema_version: str = "0.1"
    provider: Optional[str] = None
    model: Optional[str] = None
    window: Optional[Dict[str, Any]] = None
    dimensions: Optional[Dict[str, Any]] = None
    revision: Optional[Dict[str, Any]] = None

    def validate(self, line_number: Optional[int] = None) -> None:
        """Strict validation according to US-03.a, US-03.b, DQ01, DQ02 and schema."""
        # 1. Required fields
        required_attrs = [
            ("measurement_id", self.measurement_id),
            ("event_id", self.event_id),
            ("metric", self.metric),
            ("value", self.value),
            ("unit", self.unit),
            ("aggregation", self.aggregation),
            ("source", self.source),
            ("source_account_id", self.source_account_id),
            ("provenance", self.provenance),
            ("evidence_ids", self.evidence_ids),
            ("collector_version", self.collector_version),
            ("mapping_version", self.mapping_version),
        ]
        for name, val in required_attrs:
            if val is None or (isinstance(val, str) and not val.strip()):
                raise ValidationError(f"Missing required field '{name}'", field_name=name, line_number=line_number)

        if not ULID_PATTERN.match(self.measurement_id):
            raise ValidationError(f"Invalid measurement_id: {self.measurement_id}", "measurement_id", line_number)
        if not ULID_PATTERN.match(self.event_id):
            raise ValidationError(f"Invalid event_id: {self.event_id}", "event_id", line_number)
        if not ALIAS_PATTERN.match(self.source):
            raise ValidationError(f"Invalid source: {self.source}", "source", line_number)
        if not ALIAS_PATTERN.match(self.source_account_id):
            raise ValidationError(f"Invalid source_account_id: {self.source_account_id}", "source_account_id", line_number)
        if not SEMVER_PATTERN.match(self.mapping_version):
            raise ValidationError(f"Invalid mapping_version semver: {self.mapping_version}", "mapping_version", line_number)

        # 2. Metric and unit checks
        if self.metric not in ALLOWED_METRICS:
            raise ValidationError(f"Unknown metric '{self.metric}'", "metric", line_number)
        if self.unit not in ALLOWED_UNITS:
            raise ValidationError(f"Unknown unit '{self.unit}'", "unit", line_number)
        if self.aggregation not in ALLOWED_AGGREGATIONS:
            raise ValidationError(f"Unknown aggregation '{self.aggregation}'", "aggregation", line_number)

        # 3. Numeric value validation: no floats, no NaN/Infinity, must be finite Decimal or integer
        if isinstance(self.value, float):
            raise ValidationError("Float value forbidden (use integer or exact decimal string)", "value", line_number)
        if isinstance(self.value, bool):
            raise ValidationError("Boolean value forbidden", "value", line_number)

        try:
            d_val = Decimal(str(self.value))
        except (InvalidOperation, ValueError, TypeError):
            raise ValidationError(f"Unparseable numeric value: {self.value}", "value", line_number)

        if not d_val.is_finite():
            raise ValidationError(f"Non-finite numeric value: {self.value}", "value", line_number)

        if d_val.is_zero():
            d_val = Decimal(0)

        # Range and percentage checks
        if self.metric in PERCENT_METRICS or self.unit == "percent":
            if not (Decimal("0") <= d_val <= Decimal("100")):
                raise ValidationError(f"Invalid percentage value {d_val}: must be between 0 and 100", "value", line_number)

        if self.metric in NON_NEGATIVE_METRICS and d_val < Decimal("0"):
            raise ValidationError(f"Metric '{self.metric}' cannot have negative value: {d_val}", "value", line_number)

        if self.unit in ("token", "request") and d_val != d_val.to_integral_value():
            raise ValidationError(f"Fractional count not allowed for unit '{self.unit}': {d_val}", "value", line_number)

        # 4. Provenance validation
        if not isinstance(self.provenance, Provenance):
            raise ValidationError("provenance must be a valid Provenance object", "provenance", line_number)
        self.provenance.validate()

        # 5. Evidence references: at least 1 resolvable reference (US-03.a, minItems 1)
        if not isinstance(self.evidence_ids, (list, tuple)) or len(self.evidence_ids) == 0:
            raise ValidationError("At least one evidence reference is required (minItems: 1)", "evidence_ids", line_number)
        for ev_id in self.evidence_ids:
            if not EVIDENCE_ID_PATTERN.match(ev_id):
                raise ValidationError(f"Invalid evidence_id format: '{ev_id}'", "evidence_ids", line_number)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "measurement_id": self.measurement_id,
            "event_id": self.event_id,
            "metric": self.metric,
            "value": str(self.value) if isinstance(self.value, Decimal) else self.value,
            "unit": self.unit,
            "aggregation": self.aggregation,
            "source": self.source,
            "source_account_id": self.source_account_id,
            "provider": self.provider,
            "model": self.model,
            "window": self.window,
            "dimensions": self.dimensions or {},
            "provenance": self.provenance.to_dict(),
            "evidence_ids": list(self.evidence_ids),
            "collector_version": self.collector_version,
            "mapping_version": self.mapping_version,
            "revision": self.revision or {
                "revision_id": generate_ulid(),
                "revision_sequence": 1,
                "replaces_revision_id": None,
                "reason": "initial",
                "revised_at": utc_now_iso(),
            },
        }


@dataclass
class RejectionRecord:
    line_number: Optional[int]
    raw_record: Any
    field_name: Optional[str]
    reason: str
    rejected_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "line_number": self.line_number,
            "raw_record": self.raw_record,
            "field_name": self.field_name,
            "reason": self.reason,
            "rejected_at": self.rejected_at,
        }


@dataclass
class ImportReport:
    total_records: int = 0
    accepted_records: int = 0
    rejected_records: int = 0
    rejections: List[RejectionRecord] = field(default_factory=list)

    def add_rejection(self, line_number: Optional[int], raw_record: Any, field_name: Optional[str], reason: str) -> None:
        self.rejected_records += 1
        self.rejections.append(RejectionRecord(
            line_number=line_number,
            raw_record=raw_record,
            field_name=field_name,
            reason=reason,
        ))

    def add_accepted(self) -> None:
        self.accepted_records += 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_records": self.total_records,
            "accepted_records": self.accepted_records,
            "rejected_records": self.rejected_records,
            "rejections": [r.to_dict() for r in self.rejections],
        }


@dataclass
class VerificationResult:
    evidence_id: str
    path: str
    expected_sha256: str
    actual_sha256: str
    verified: bool
    size_bytes: int
    is_redacted: bool
    error: Optional[str] = None


class EvidenceStore:
    """
    Immutable Evidence & Raw Payload Store.
    Operates over:
      - Raw artifact files on filesystem (<base_dir>/artifacts/<date>/<filename>)
      - SQLite database for indexing, provenance binding and active views
    Enforces US-03.a, US-03.b, US-03.c, DQ01, DQ02.
    """

    def __init__(self, base_dir: Union[str, Path], db_path: Optional[Union[str, Path]] = None):
        self.base_dir = Path(base_dir).resolve()
        self.artifacts_dir = self.base_dir / "artifacts"
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

        if db_path is None:
            self.db_path = self.base_dir / "ael_evidence.db"
        else:
            self.db_path = Path(db_path).resolve()
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS raw_evidence (
                id TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL,
                source_id TEXT NOT NULL,
                account_id TEXT NOT NULL,
                path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                mime_type TEXT NOT NULL,
                collector_version TEXT NOT NULL,
                capture_point TEXT NOT NULL,
                is_redacted INTEGER NOT NULL DEFAULT 0,
                redaction_version TEXT,
                redaction_reason TEXT,
                created_at TEXT NOT NULL,
                observed_at TEXT,
                metadata_json TEXT
            );

            CREATE TABLE IF NOT EXISTS measurements (
                id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                metric TEXT NOT NULL,
                value TEXT NOT NULL,
                unit TEXT NOT NULL,
                aggregation TEXT NOT NULL,
                source_id TEXT NOT NULL,
                account_id TEXT NOT NULL,
                provider TEXT,
                model TEXT,
                provenance_class TEXT NOT NULL,
                provenance_method TEXT NOT NULL,
                confidence REAL NOT NULL,
                confidence_reason TEXT NOT NULL,
                validation_status TEXT NOT NULL,
                collector_version TEXT NOT NULL,
                mapping_version TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                quarantine_reason TEXT,
                created_at TEXT NOT NULL,
                payload_json TEXT
            );

            CREATE TABLE IF NOT EXISTS measurement_evidence (
                measurement_id TEXT NOT NULL,
                evidence_id TEXT NOT NULL,
                PRIMARY KEY (measurement_id, evidence_id),
                FOREIGN KEY (measurement_id) REFERENCES measurements(id) ON DELETE CASCADE,
                FOREIGN KEY (evidence_id) REFERENCES raw_evidence(id) ON DELETE RESTRICT
            );

            CREATE TABLE IF NOT EXISTS audit_rejections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                line_number INTEGER,
                raw_record TEXT,
                field_name TEXT,
                reason TEXT NOT NULL,
                rejected_at TEXT NOT NULL
            );
            """)

    def store_raw_payload(
        self,
        payload_bytes: bytes,
        source: str,
        account: str,
        mime_type: str,
        collector_version: str,
        capture_point: str = "provider_response_verbatim",
        redaction: Optional[RedactionInfo] = None,
        observed_at: Optional[str] = None,
        file_ext: str = "json",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> EvidenceArtifact:
        """
        Store a raw immutable payload into filesystem and index in SQLite.
        Guarantees that once written, payload is read-only and hash-locked.
        """
        if redaction is None:
            redaction = RedactionInfo(redacted=False)

        sha = compute_sha256(payload_bytes)
        size = len(payload_bytes)
        created_at = utc_now_iso()
        evidence_id = generate_evidence_id()

        artifact = EvidenceArtifact(
            evidence_id=evidence_id,
            sha256=sha,
            size_bytes=size,
            mime_type=mime_type,
            source=source,
            account=account,
            created_at=created_at,
            observed_at=observed_at,
            collector_version=collector_version,
            capture_point=capture_point,
            redaction=redaction,
            metadata_json=metadata,
        )
        artifact.validate()

        # Filename convention: <timestamp>_<source>_<account>_<evidence_id_short>_<sha256-prefix>.<ext>
        safe_source = re.sub(r"[^a-zA-Z0-9_-]", "", source)
        safe_account = re.sub(r"[^a-zA-Z0-9_-]", "", account)
        date_folder = created_at[:10]  # YYYY-MM-DD
        target_dir = self.artifacts_dir / date_folder
        target_dir.mkdir(parents=True, exist_ok=True)

        filename = f"{created_at.replace(':', '-')}_{safe_source}_{safe_account}_{evidence_id[-8:]}_{sha[:8]}.{file_ext}"
        target_path = target_dir / filename
        relpath = str(target_path.relative_to(self.base_dir))

        if target_path.exists():
            raise ImmutableStorageError(f"Target artifact path already exists: {target_path}")

        # Write payload
        with open(target_path, "wb") as f:
            f.write(payload_bytes)

        # Set file read-only on OS level (0o444)
        os.chmod(target_path, 0o444)

        artifact.stored_relpath = relpath

        # Insert into SQLite
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO raw_evidence (
                    id, schema_version, source_id, account_id, path, sha256, size_bytes,
                    mime_type, collector_version, capture_point, is_redacted,
                    redaction_version, redaction_reason, created_at, observed_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.evidence_id,
                    artifact.schema_version,
                    artifact.source,
                    artifact.account,
                    artifact.stored_relpath,
                    artifact.sha256,
                    artifact.size_bytes,
                    artifact.mime_type,
                    artifact.collector_version,
                    artifact.capture_point,
                    1 if artifact.redaction.redacted else 0,
                    artifact.redaction.redaction_version,
                    artifact.redaction.redaction_reason,
                    artifact.created_at,
                    artifact.observed_at,
                    json.dumps(artifact.metadata_json) if artifact.metadata_json else None,
                ),
            )

        return artifact

    def get_evidence(self, evidence_id: str) -> Optional[EvidenceArtifact]:
        """Fetch evidence metadata by ID."""
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM raw_evidence WHERE id = ?", (evidence_id,)).fetchone()
            if not row:
                return None
            return self._row_to_artifact(row)

    def read_evidence_bytes(self, evidence_id: str) -> Tuple[bytes, EvidenceArtifact]:
        """Read artifact bytes and verify hash on read."""
        artifact = self.get_evidence(evidence_id)
        if not artifact:
            raise KeyError(f"Evidence {evidence_id} not found")

        full_path = self.base_dir / artifact.stored_relpath
        if not full_path.exists():
            raise FileNotFoundError(f"Underlying evidence artifact file missing: {full_path}")

        data = full_path.read_bytes()
        actual_sha = compute_sha256(data)
        if actual_sha != artifact.sha256:
            raise IntegrityError(
                f"Integrity violation on evidence {evidence_id}! Expected {artifact.sha256}, got {actual_sha}"
            )
        return data, artifact

    def verify_evidence(self, evidence_id: str) -> VerificationResult:
        """Verify the integrity of a specific evidence artifact."""
        artifact = self.get_evidence(evidence_id)
        if not artifact:
            return VerificationResult(
                evidence_id=evidence_id,
                path="",
                expected_sha256="",
                actual_sha256="",
                verified=False,
                size_bytes=0,
                is_redacted=False,
                error="Evidence ID not found in database",
            )

        full_path = self.base_dir / artifact.stored_relpath
        if not full_path.exists():
            return VerificationResult(
                evidence_id=evidence_id,
                path=artifact.stored_relpath or "",
                expected_sha256=artifact.sha256,
                actual_sha256="",
                verified=False,
                size_bytes=artifact.size_bytes,
                is_redacted=artifact.redaction.redacted,
                error=f"File not found on disk: {full_path}",
            )

        try:
            data = full_path.read_bytes()
            actual_sha = compute_sha256(data)
            is_valid = actual_sha == artifact.sha256
            return VerificationResult(
                evidence_id=evidence_id,
                path=artifact.stored_relpath or "",
                expected_sha256=artifact.sha256,
                actual_sha256=actual_sha,
                verified=is_valid,
                size_bytes=len(data),
                is_redacted=artifact.redaction.redacted,
                error=None if is_valid else f"SHA256 mismatch: expected {artifact.sha256}, found {actual_sha}",
            )
        except Exception as exc:
            return VerificationResult(
                evidence_id=evidence_id,
                path=artifact.stored_relpath or "",
                expected_sha256=artifact.sha256,
                actual_sha256="",
                verified=False,
                size_bytes=0,
                is_redacted=artifact.redaction.redacted,
                error=str(exc),
            )

    def verify_all_evidences(self) -> List[VerificationResult]:
        """Verify integrity of all stored artifacts (DQ02)."""
        with self._get_connection() as conn:
            rows = conn.execute("SELECT id FROM raw_evidence ORDER BY created_at ASC").fetchall()
        return [self.verify_evidence(r["id"]) for r in rows]

    def ingest_measurements(
        self,
        measurements: Sequence[Union[Measurement, Dict[str, Any]]],
        import_report: Optional[ImportReport] = None,
    ) -> ImportReport:
        """
        Ingest a batch of measurements into the store.
        Enforces:
          - US-03.a: Complete mandatory metadata & evidence resolution
          - US-03.b: Rejection of invalid percentages, non-finite values, missing fields
                     Rejections logged with exact reason, raw record and line/sequence index.
          - DQ01: Quarantining of invalid measurements without polluting active view.
        """
        if import_report is None:
            import_report = ImportReport()

        import_report.total_records += len(measurements)

        for idx, item in enumerate(measurements, start=1):
            line_no = idx
            # If raw dictionary, instantiate Measurement
            meas: Optional[Measurement] = None
            if isinstance(item, Measurement):
                meas = item
            elif isinstance(item, dict):
                try:
                    prov_raw = item.get("provenance")
                    if not isinstance(prov_raw, dict):
                        import_report.add_rejection(
                            line_number=line_no,
                            raw_record=item,
                            field_name="provenance",
                            reason="Missing or invalid provenance object",
                        )
                        continue

                    prov = Provenance(
                        provenance_class=prov_raw.get("class", ""),
                        method=prov_raw.get("method", ""),
                        confidence=prov_raw.get("confidence", 0.0),
                        confidence_reason=prov_raw.get("confidence_reason", ""),
                        validation_status=prov_raw.get("validation_status", "unvalidated"),
                        reconciled_at=prov_raw.get("reconciled_at"),
                    )

                    meas = Measurement(
                        schema_version=item.get("schema_version", "0.1"),
                        measurement_id=item.get("measurement_id", ""),
                        event_id=item.get("event_id", ""),
                        metric=item.get("metric", ""),
                        value=item.get("value"),
                        unit=item.get("unit", ""),
                        aggregation=item.get("aggregation", ""),
                        source=item.get("source", ""),
                        source_account_id=item.get("source_account_id", ""),
                        provenance=prov,
                        evidence_ids=item.get("evidence_ids", []),
                        collector_version=item.get("collector_version", ""),
                        mapping_version=item.get("mapping_version", ""),
                        provider=item.get("provider"),
                        model=item.get("model"),
                        window=item.get("window"),
                        dimensions=item.get("dimensions"),
                        revision=item.get("revision"),
                    )
                except Exception as ex:
                    import_report.add_rejection(
                        line_number=line_no,
                        raw_record=item,
                        field_name="format",
                        reason=f"Failed to parse measurement structure: {str(ex)}",
                    )
                    continue
            else:
                import_report.add_rejection(
                    line_number=line_no,
                    raw_record=item,
                    field_name="record",
                    reason=f"Expected Measurement or dict, got {type(item).__name__}",
                )
                continue

            # Validate measurement fields & business invariants
            try:
                meas.validate(line_number=line_no)
            except ValidationError as ve:
                import_report.add_rejection(
                    line_number=line_no,
                    raw_record=item if isinstance(item, dict) else meas.to_dict(),
                    field_name=ve.field_name,
                    reason=str(ve),
                )
                continue

            # Verify that all referenced evidence IDs exist and resolve in storage (US-03.a)
            unresolved = []
            for ev_id in meas.evidence_ids:
                if self.get_evidence(ev_id) is None:
                    unresolved.append(ev_id)
            if unresolved:
                import_report.add_rejection(
                    line_number=line_no,
                    raw_record=item if isinstance(item, dict) else meas.to_dict(),
                    field_name="evidence_ids",
                    reason=f"Unresolvable evidence reference(s): {', '.join(unresolved)}",
                )
                continue

            # Insert measurement into SQLite active view
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO measurements (
                        id, event_id, metric, value, unit, aggregation,
                        source_id, account_id, provider, model,
                        provenance_class, provenance_method, confidence,
                        confidence_reason, validation_status, collector_version,
                        mapping_version, is_active, created_at, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        meas.measurement_id,
                        meas.event_id,
                        meas.metric,
                        str(meas.value) if isinstance(meas.value, Decimal) else str(meas.value),
                        meas.unit,
                        meas.aggregation,
                        meas.source,
                        meas.source_account_id,
                        meas.provider,
                        meas.model,
                        meas.provenance.provenance_class,
                        meas.provenance.method,
                        meas.provenance.confidence,
                        meas.provenance.confidence_reason,
                        meas.provenance.validation_status,
                        meas.collector_version,
                        meas.mapping_version,
                        utc_now_iso(),
                        json.dumps(meas.to_dict()),
                    ),
                )
                for ev_id in meas.evidence_ids:
                    conn.execute(
                        "INSERT INTO measurement_evidence (measurement_id, evidence_id) VALUES (?, ?)",
                        (meas.measurement_id, ev_id),
                    )

            import_report.add_accepted()

        return import_report

    def get_active_measurements(self, metric: Optional[str] = None, source: Optional[str] = None) -> List[Measurement]:
        """Fetch active verified measurements."""
        query = "SELECT payload_json FROM measurements WHERE is_active = 1"
        params: List[Any] = []
        if metric:
            query += " AND metric = ?"
            params.append(metric)
        if source:
            query += " AND source_id = ?"
            params.append(source)
        query += " ORDER BY created_at ASC"

        with self._get_connection() as conn:
            rows = conn.execute(query, params).fetchall()

        results = []
        for r in rows:
            data = json.loads(r["payload_json"])
            prov = Provenance(
                provenance_class=data["provenance"]["class"],
                method=data["provenance"]["method"],
                confidence=data["provenance"]["confidence"],
                confidence_reason=data["provenance"]["confidence_reason"],
                validation_status=data["provenance"]["validation_status"],
                reconciled_at=data["provenance"].get("reconciled_at"),
            )
            meas = Measurement(
                schema_version=data.get("schema_version", "0.1"),
                measurement_id=data["measurement_id"],
                event_id=data["event_id"],
                metric=data["metric"],
                value=Decimal(str(data["value"])),
                unit=data["unit"],
                aggregation=data["aggregation"],
                source=data["source"],
                source_account_id=data["source_account_id"],
                provider=data.get("provider"),
                model=data.get("model"),
                window=data.get("window"),
                dimensions=data.get("dimensions"),
                provenance=prov,
                evidence_ids=data["evidence_ids"],
                collector_version=data["collector_version"],
                mapping_version=data["mapping_version"],
                revision=data.get("revision"),
            )
            results.append(meas)
        return results

    def trace_number_to_evidence(self, measurement_id: str) -> Dict[str, Any]:
        """
        US-03.a / US-03.c: Trace any reported measurement number back to its immutable raw evidence.
        Includes live integrity verification of all bound evidence artifacts.
        If any artifact fails verification, flags the receipt as 'unverified' (US-03.c).
        """
        with self._get_connection() as conn:
            meas_row = conn.execute("SELECT * FROM measurements WHERE id = ?", (measurement_id,)).fetchone()
            if not meas_row:
                raise KeyError(f"Measurement {measurement_id} not found")

            ev_rows = conn.execute(
                """
                SELECT re.* FROM raw_evidence re
                JOIN measurement_evidence me ON me.evidence_id = re.id
                WHERE me.measurement_id = ?
                """,
                (measurement_id,),
            ).fetchall()

        bound_evidences = []
        overall_verified = True
        verification_details = []

        for row in ev_rows:
            artifact = self._row_to_artifact(row)
            ver = self.verify_evidence(artifact.evidence_id)
            if not ver.verified:
                overall_verified = False
            verification_details.append(ver)
            bound_evidences.append({
                "artifact": artifact.to_descriptor(),
                "verification": asdict(ver),
                "is_redacted": artifact.redaction.redacted,
                "redaction_version": artifact.redaction.redaction_version,
            })

        return {
            "measurement_id": meas_row["id"],
            "metric": meas_row["metric"],
            "value": meas_row["value"],
            "unit": meas_row["unit"],
            "source": meas_row["source_id"],
            "account": meas_row["account_id"],
            "collector_version": meas_row["collector_version"],
            "mapping_version": meas_row["mapping_version"],
            "provenance": {
                "class": meas_row["provenance_class"],
                "method": meas_row["provenance_method"],
                "confidence": meas_row["confidence"],
                "confidence_reason": meas_row["confidence_reason"],
                "validation_status": meas_row["validation_status"],
            },
            "evidence_count": len(bound_evidences),
            "evidences": bound_evidences,
            # US-03.c: Derived reports mark the affected receipt as unverified when integrity fails
            "audit_status": "verified" if overall_verified else "unverified",
            "integrity_violation": not overall_verified,
        }

    def generate_derived_report(self, metric: str) -> Dict[str, Any]:
        """
        Generate an aggregated report over active measurements.
        Marks any aggregate as 'unverified' if any contributing evidence has an integrity breach (US-03.c).
        """
        measurements = self.get_active_measurements(metric=metric)
        total = Decimal(0)
        contributing_measurements = []
        all_verified = True

        for m in measurements:
            total += m.value
            trace = self.trace_number_to_evidence(m.measurement_id)
            if trace["audit_status"] != "verified":
                all_verified = False
            contributing_measurements.append(trace)

        return {
            "metric": metric,
            "count": len(measurements),
            "total_value": str(total),
            "contributing_measurements": contributing_measurements,
            "report_verification_status": "verified" if all_verified else "unverified",
            "has_integrity_breach": not all_verified,
        }

    def _row_to_artifact(self, row: sqlite3.Row) -> EvidenceArtifact:
        redaction = RedactionInfo(
            redacted=bool(row["is_redacted"]),
            redaction_version=row["redaction_version"],
            redaction_reason=row["redaction_reason"],
        )
        metadata = json.loads(row["metadata_json"]) if row["metadata_json"] else None
        return EvidenceArtifact(
            schema_version=row["schema_version"],
            evidence_id=row["id"],
            sha256=row["sha256"],
            size_bytes=row["size_bytes"],
            mime_type=row["mime_type"],
            source=row["source_id"],
            account=row["account_id"],
            created_at=row["created_at"],
            observed_at=row["observed_at"],
            collector_version=row["collector_version"],
            capture_point=row["capture_point"],
            redaction=redaction,
            stored_relpath=row["path"],
            metadata_json=metadata,
        )

    def export_evidence_bundle(
        self,
        export_path: Union[str, Path],
        measurement_ids: Optional[Sequence[str]] = None,
        bundle_format: str = "tar.gz",
        transformation_version: str = "0.1.0",
        notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        US-03.d: Exportierbares Evidenzbündel mit Prüfsummen für externe Prüfer.
        Packt Belegdateien, Messungen, Lineage und Manifest in ein portables Archiv (tar.gz oder zip).
        """
        export_path = Path(export_path).resolve()
        export_path.parent.mkdir(parents=True, exist_ok=True)

        # 1. Gather measurements
        if measurement_ids is not None:
            active_measurements = []
            for m_id in measurement_ids:
                with self._get_connection() as conn:
                    row = conn.execute("SELECT payload_json FROM measurements WHERE id = ?", (m_id,)).fetchone()
                    if not row:
                        raise KeyError(f"Measurement {m_id} not found")
                    data = json.loads(row["payload_json"])
                    prov = Provenance(
                        provenance_class=data["provenance"]["class"],
                        method=data["provenance"]["method"],
                        confidence=data["provenance"]["confidence"],
                        confidence_reason=data["provenance"]["confidence_reason"],
                        validation_status=data["provenance"]["validation_status"],
                        reconciled_at=data["provenance"].get("reconciled_at"),
                    )
                    meas = Measurement(
                        schema_version=data.get("schema_version", "0.1"),
                        measurement_id=data["measurement_id"],
                        event_id=data["event_id"],
                        metric=data["metric"],
                        value=Decimal(str(data["value"])),
                        unit=data["unit"],
                        aggregation=data["aggregation"],
                        source=data["source"],
                        source_account_id=data["source_account_id"],
                        provider=data.get("provider"),
                        model=data.get("model"),
                        window=data.get("window"),
                        dimensions=data.get("dimensions"),
                        provenance=prov,
                        evidence_ids=data["evidence_ids"],
                        collector_version=data["collector_version"],
                        mapping_version=data["mapping_version"],
                        revision=data.get("revision"),
                    )
                    active_measurements.append(meas)
        else:
            active_measurements = self.get_active_measurements()

        # 2. Gather unique evidence IDs referenced by measurements
        referenced_evidence_ids = set()
        for m in active_measurements:
            for ev_id in m.evidence_ids:
                referenced_evidence_ids.add(ev_id)

        evidence_artifacts: List[EvidenceArtifact] = []
        evidence_entries = []
        for ev_id in sorted(referenced_evidence_ids):
            art = self.get_evidence(ev_id)
            if not art:
                raise KeyError(f"Referenced evidence {ev_id} not found in store")
            evidence_artifacts.append(art)
            ver = self.verify_evidence(ev_id)
            evidence_entries.append({
                "evidence_id": art.evidence_id,
                "sha256": art.sha256,
                "size_bytes": art.size_bytes,
                "mime_type": art.mime_type,
                "source": art.source,
                "account": art.account,
                "capture_point": art.capture_point,
                "collector_version": art.collector_version,
                "is_redacted": art.redaction.redacted,
                "redaction_version": art.redaction.redaction_version,
                "verified_before_export": ver.verified,
                "bundle_relpath": f"evidence/{art.evidence_id}.bin",
            })

        bundle_id = f"bnd_{generate_ulid()}"
        bundle_manifest = {
            "schema_version": "0.1",
            "bundle_id": bundle_id,
            "created_at": utc_now_iso(),
            "transformation_version": transformation_version,
            "notes": notes or "AEL auditable evidence bundle for external verification",
            "measurement_count": len(active_measurements),
            "evidence_count": len(evidence_artifacts),
            "measurements": [m.to_dict() for m in active_measurements],
            "evidences": evidence_entries,
        }

        # Calculate a deterministic manifest content hash excluding mutable runtime markers if needed
        manifest_bytes = json.dumps(bundle_manifest, indent=2, sort_keys=True).encode("utf-8")
        manifest_sha = compute_sha256(manifest_bytes)
        bundle_manifest["manifest_sha256"] = manifest_sha

        # Re-encode manifest with manifest_sha256
        final_manifest_bytes = json.dumps(bundle_manifest, indent=2, sort_keys=True).encode("utf-8")

        # Create archive
        if bundle_format in ("tar.gz", "tgz", "tar"):
            mode = "w:gz" if bundle_format in ("tar.gz", "tgz") else "w"
            with tarfile.open(export_path, mode) as tar:
                # Add manifest.json
                ti_manifest = tarfile.TarInfo(name="manifest.json")
                ti_manifest.size = len(final_manifest_bytes)
                ti_manifest.mtime = int(time.time())
                ti_manifest.mode = 0o444
                import io
                tar.addfile(ti_manifest, io.BytesIO(final_manifest_bytes))

                # Add evidence files
                for art in evidence_artifacts:
                    data, _ = self.read_evidence_bytes(art.evidence_id)
                    ti_file = tarfile.TarInfo(name=f"evidence/{art.evidence_id}.bin")
                    ti_file.size = len(data)
                    ti_file.mtime = int(time.time())
                    ti_file.mode = 0o444
                    tar.addfile(ti_file, io.BytesIO(data))
        elif bundle_format == "zip":
            with zipfile.ZipFile(export_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("manifest.json", final_manifest_bytes)
                for art in evidence_artifacts:
                    data, _ = self.read_evidence_bytes(art.evidence_id)
                    zf.writestr(f"evidence/{art.evidence_id}.bin", data)
        else:
            raise ValueError(f"Unsupported bundle format: '{bundle_format}'. Must be 'tar.gz', 'tar', or 'zip'.")

        bundle_bytes = export_path.read_bytes()
        bundle_sha256 = compute_sha256(bundle_bytes)

        return {
            "bundle_id": bundle_id,
            "bundle_path": str(export_path),
            "bundle_sha256": bundle_sha256,
            "format": bundle_format,
            "manifest_sha256": manifest_sha,
            "measurement_count": len(active_measurements),
            "evidence_count": len(evidence_artifacts),
            "manifest": bundle_manifest,
        }

    @staticmethod
    def verify_evidence_bundle(bundle_path: Union[str, Path]) -> Dict[str, Any]:
        """
        US-03.d: Unabhängige Verifikation von Belegbündeln gegen das Manifest.
        Prüft:
          1. Existenz & Schema des Manifests
          2. Vollständigkeit aller referenzierten Belegdateien im Archiv
          3. Exakte SHA-256 Prüfsummenübereinstimmung jeder einzelnen Belegdatei
          4. Übereinstimmung der Beleg-Bindungen aller Messungen
          5. Gesamturteil (audit_verdict: PASS / FAIL)
        """
        bundle_path = Path(bundle_path).resolve()
        if not bundle_path.exists():
            return {
                "verified": False,
                "audit_verdict": "FAIL",
                "error": f"Bundle file not found: {bundle_path}",
                "evidence_checks": [],
                "measurement_checks": [],
            }

        manifest: Optional[Dict[str, Any]] = None
        evidence_files: Dict[str, bytes] = {}

        # Detect format & read entries
        try:
            if tarfile.is_tarfile(bundle_path):
                with tarfile.open(bundle_path, "r:*") as tar:
                    for member in tar.getmembers():
                        if member.isfile():
                            f = tar.extractfile(member)
                            if f:
                                data = f.read()
                                if member.name == "manifest.json":
                                    manifest = json.loads(data.decode("utf-8"))
                                else:
                                    evidence_files[member.name] = data
            elif zipfile.is_zipfile(bundle_path):
                with zipfile.ZipFile(bundle_path, "r") as zf:
                    for name in zf.namelist():
                        data = zf.read(name)
                        if name == "manifest.json":
                            manifest = json.loads(data.decode("utf-8"))
                        else:
                            evidence_files[name] = data
            else:
                return {
                    "verified": False,
                    "audit_verdict": "FAIL",
                    "error": f"Unsupported or corrupted archive format: {bundle_path}",
                    "evidence_checks": [],
                    "measurement_checks": [],
                }
        except Exception as exc:
            return {
                "verified": False,
                "audit_verdict": "FAIL",
                "error": f"Failed to read archive: {exc}",
                "evidence_checks": [],
                "measurement_checks": [],
            }

        if manifest is None:
            return {
                "verified": False,
                "audit_verdict": "FAIL",
                "error": "manifest.json missing in evidence bundle",
                "evidence_checks": [],
                "measurement_checks": [],
            }

        all_verified = True
        evidence_checks = []
        manifest_evidences = manifest.get("evidences", [])

        # Verify evidence files against manifest
        for ev in manifest_evidences:
            ev_id = ev.get("evidence_id")
            expected_sha = ev.get("sha256")
            relpath = ev.get("bundle_relpath") or f"evidence/{ev_id}.bin"

            if relpath not in evidence_files:
                all_verified = False
                evidence_checks.append({
                    "evidence_id": ev_id,
                    "expected_sha256": expected_sha,
                    "actual_sha256": None,
                    "verified": False,
                    "error": f"Missing file in bundle: {relpath}",
                })
                continue

            file_bytes = evidence_files[relpath]
            actual_sha = compute_sha256(file_bytes)
            is_match = (actual_sha == expected_sha)
            if not is_match:
                all_verified = False

            evidence_checks.append({
                "evidence_id": ev_id,
                "bundle_relpath": relpath,
                "expected_sha256": expected_sha,
                "actual_sha256": actual_sha,
                "verified": is_match,
                "size_bytes": len(file_bytes),
                "error": None if is_match else f"SHA256 mismatch: expected {expected_sha}, got {actual_sha}",
            })

        # Verify measurement evidence references
        measurement_checks = []
        known_evidence_ids = {ev.get("evidence_id") for ev in manifest_evidences}
        for meas in manifest.get("measurements", []):
            meas_id = meas.get("measurement_id")
            meas_ev_ids = meas.get("evidence_ids", [])
            missing_refs = [eid for eid in meas_ev_ids if eid not in known_evidence_ids]
            valid_refs = (len(missing_refs) == 0 and len(meas_ev_ids) > 0)
            if not valid_refs:
                all_verified = False
            measurement_checks.append({
                "measurement_id": meas_id,
                "metric": meas.get("metric"),
                "value": meas.get("value"),
                "mapping_version": meas.get("mapping_version"),
                "collector_version": meas.get("collector_version"),
                "referenced_evidence_ids": meas_ev_ids,
                "missing_evidence_ids": missing_refs,
                "verified": valid_refs,
            })

        return {
            "verified": all_verified,
            "audit_verdict": "PASS" if all_verified else "FAIL",
            "bundle_id": manifest.get("bundle_id"),
            "transformation_version": manifest.get("transformation_version"),
            "total_evidences": len(manifest_evidences),
            "verified_evidences": sum(1 for c in evidence_checks if c["verified"]),
            "total_measurements": len(manifest.get("measurements", [])),
            "verified_measurements": sum(1 for m in measurement_checks if m["verified"]),
            "evidence_checks": evidence_checks,
            "measurement_checks": measurement_checks,
        }

    def generate_audit_report(self) -> Dict[str, Any]:
        """
        US-03.d: Audit-Bericht mit Integritätsnachweis über alle Belege und Messungen.
        Überprüft alle gespeicherten Rohbelege auf Manipulation, berechnet Belegprüfsummen
        und liefert einen vollständigen Audit-Status für externe Prüfer.
        """
        ev_results = self.verify_all_evidences()
        total_evidences = len(ev_results)
        verified_evidences = sum(1 for r in ev_results if r.verified)
        integrity_breaches = [asdict(r) for r in ev_results if not r.verified]

        with self._get_connection() as conn:
            meas_rows = conn.execute("SELECT * FROM measurements WHERE is_active = 1").fetchall()
            rejections_count = conn.execute("SELECT COUNT(*) FROM audit_rejections").fetchone()[0]

        total_measurements = len(meas_rows)
        all_passed = (total_evidences == verified_evidences and len(integrity_breaches) == 0)

        return {
            "schema_version": "0.1",
            "audit_timestamp": utc_now_iso(),
            "audit_verdict": "PASS" if all_passed else "FAIL",
            "integrity_summary": {
                "total_evidences": total_evidences,
                "verified_evidences": verified_evidences,
                "breach_count": len(integrity_breaches),
                "dq02_compliance_percent": "100.0" if total_evidences == 0 else f"{(verified_evidences / total_evidences) * 100:.2f}",
            },
            "rejections_in_quarantine": rejections_count,
            "active_measurements_count": total_measurements,
            "integrity_breaches": integrity_breaches,
            "evidence_catalog": [
                {
                    "evidence_id": r.evidence_id,
                    "expected_sha256": r.expected_sha256,
                    "actual_sha256": r.actual_sha256,
                    "verified": r.verified,
                    "is_redacted": r.is_redacted,
                    "size_bytes": r.size_bytes,
                }
                for r in ev_results
            ],
        }
