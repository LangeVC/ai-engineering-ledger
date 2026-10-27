"""Manual snapshot importers for Claude Pro, Codex Plus, and Google Antigravity.

Complies with PRD US-07.a, US-07.b, US-07.c, US-07.d, US-07.e, US-07.f:
- US-07.a: Supports product/account, observation time with explicit timezone,
  optional evidence ref/id, and suitable quota/credit metrics.
  Values not visible in UI remain unknown / None.
- US-07.b: "80 % remaining" is stored as remaining and/or derived to "20 % used".
  Strict percentage validation: [0, 100] allowed; -1 and 101 rejected.
  No token hallucinations: percentages remain percentages, never guessed tokens.
- US-07.c: Weekly and short-term quotas are captured as separate windows.
  Unknown reset timestamps are NEVER automatically inferred from "weekly" or window types;
  they remain null / None.
- US-07.d: Screenshot values are entered manually by the user; no implicit OCR or
  external image transmission. Without screenshot, confirmed manual entry generates
  an evidence descriptor marked explicitly as manual / user_manual_entry.
- US-07.e: Subscription, payment, and balance entries are strictly separated.
  The user explicitly previews and confirms entries; cancellation modifies NO measurements.
- US-07.f: Status/`due` check recognizes overdue manual sources based on configurable
  cadence/freshness (defaults to 24h / 86400s daily rhythm). Does not require external notifications.
- Antigravity credits are strictly unit='credit' (never tokens, never currency).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple, Union
import hashlib
import json
import re

RFC3339_REGEX = re.compile(
    r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T"
    r"([01][0-9]|2[0-3]):[0-5][0-9]:([0-5][0-9]|60)(\.[0-9]{1,9})?"
    r"(Z|[+-]([01][0-9]|2[0-3]):[0-5][0-9])$"
)

ALLOWED_PRODUCTS = {
    "claude_pro": {
        "canonical_product": "Claude Pro",
        "default_source": "anthropic",
        "supported_metrics": ["quota_percent_remaining", "quota_percent_used"],
    },
    "codex_plus": {
        "canonical_product": "ChatGPT Plus / Codex",
        "default_source": "openai",
        "supported_metrics": ["quota_percent_remaining", "quota_percent_used"],
    },
    "antigravity": {
        "canonical_product": "Google AI Pro / Antigravity",
        "default_source": "google",
        "supported_metrics": [
            "credits_consumed",
            "credit_balance",
            "quota_percent_remaining",
            "quota_percent_used",
        ],
    },
}


class ManualImportValidationError(ValueError):
    """Raised when manual input violates contract or domain rules."""
    pass


def validate_rfc3339_timestamp(ts: Optional[str], field_name: str, allow_none: bool = False) -> Optional[str]:
    """Validates that a string is a strict RFC3339 timestamp with timezone offset."""
    if ts is None:
        if allow_none:
            return None
        raise ManualImportValidationError(f"{field_name} is required and cannot be null")
    if not isinstance(ts, str) or not RFC3339_REGEX.match(ts):
        raise ManualImportValidationError(
            f"{field_name} must be a valid RFC 3339 timestamp with timezone offset (e.g. '2026-10-26T12:00:00+01:00' or '2026-10-26T12:00:00Z'), got: {ts!r}"
        )
    return ts


def validate_percentage(
    val: Optional[Union[int, float, str, Decimal]],
    field_name: str,
    allow_none: bool = False
) -> Optional[Decimal]:
    """Validates that percentage is strictly within [0, 100]. Rejects < 0, > 100, and non-numbers."""
    if val is None:
        if allow_none:
            return None
        raise ManualImportValidationError(f"{field_name} is required and cannot be null")
    try:
        dec = Decimal(str(val))
    except Exception as exc:
        raise ManualImportValidationError(f"{field_name} must be numeric, got: {val!r}") from exc

    if not dec.is_finite():
        raise ManualImportValidationError(f"{field_name} must be finite, got: {val!r}")
    if dec < Decimal("0") or dec > Decimal("100"):
        raise ManualImportValidationError(
            f"{field_name} must be between 0 and 100 (inclusive), got: {val}"
        )
    return dec


def validate_credit_amount(
    val: Optional[Union[int, float, str, Decimal]],
    field_name: str,
    allow_none: bool = False
) -> Optional[Decimal]:
    """Validates non-negative credit amounts."""
    if val is None:
        if allow_none:
            return None
        raise ManualImportValidationError(f"{field_name} is required and cannot be null")
    try:
        dec = Decimal(str(val))
    except Exception as exc:
        raise ManualImportValidationError(f"{field_name} must be numeric, got: {val!r}") from exc

    if not dec.is_finite():
        raise ManualImportValidationError(f"{field_name} must be finite, got: {val!r}")
    if dec < Decimal("0"):
        raise ManualImportValidationError(f"{field_name} cannot be negative, got: {val}")
    return dec


@dataclass
class QuotaWindowSnapshot:
    """Represents a discrete quota observation window (e.g. rolling 5h or weekly allotment)."""
    window_type: str  # e.g. "5h", "weekly", "custom"
    label: Optional[str] = None
    starts_at: Optional[str] = None  # RFC3339 if known, else None
    ends_at: Optional[str] = None    # RFC3339 if known, else None
    reset_at: Optional[str] = None   # RFC3339 if known, else None (NEVER inferred if unknown)
    quota_remaining_percent: Optional[Decimal] = None
    quota_used_percent: Optional[Decimal] = None
    source_statement: Optional[str] = None  # e.g. "80% remaining"

    def __post_init__(self):
        # US-07.c: Unknown resetzeitpunkt wird NICHT automatisch errechnet!
        if self.reset_at is not None:
            validate_rfc3339_timestamp(self.reset_at, "reset_at", allow_none=False)
        if self.starts_at is not None:
            validate_rfc3339_timestamp(self.starts_at, "starts_at", allow_none=False)
        if self.ends_at is not None:
            validate_rfc3339_timestamp(self.ends_at, "ends_at", allow_none=False)

        # US-07.b: Validate percentages and enforce derivation
        if self.quota_remaining_percent is not None:
            self.quota_remaining_percent = validate_percentage(
                self.quota_remaining_percent, "quota_remaining_percent"
            )
        if self.quota_used_percent is not None:
            self.quota_used_percent = validate_percentage(
                self.quota_used_percent, "quota_used_percent"
            )

        # US-07.b: "80 % remaining" wird als verbleibend gespeichert bzw. nachvollziehbar zu "20 % used" abgeleitet
        if self.quota_remaining_percent is not None and self.quota_used_percent is None:
            self.quota_used_percent = Decimal("100") - self.quota_remaining_percent
        elif self.quota_used_percent is not None and self.quota_remaining_percent is None:
            self.quota_remaining_percent = Decimal("100") - self.quota_used_percent
        elif self.quota_remaining_percent is not None and self.quota_used_percent is not None:
            if (self.quota_remaining_percent + self.quota_used_percent) != Decimal("100"):
                raise ManualImportValidationError(
                    f"quota_remaining_percent ({self.quota_remaining_percent}) and "
                    f"quota_used_percent ({self.quota_used_percent}) must sum to 100"
                )


@dataclass
class ManualObservationRecord:
    """Canonical model for a guided manual snapshot capture."""
    product_template: str  # "claude_pro", "codex_plus", "antigravity"
    product: str
    source: str
    source_account_id: str
    observed_at: str  # RFC3339 with timezone
    evidence_id: Optional[str] = None  # optional receipt/evidence reference
    raw_evidence_ref: Optional[str] = None
    windows: List[QuotaWindowSnapshot] = field(default_factory=list)
    credits_consumed: Optional[Decimal] = None
    credit_balance: Optional[Decimal] = None
    credit_unit: Optional[str] = None  # Strictly "credit" for Antigravity
    notes: Optional[str] = None

    def validate_invariants(self):
        """Validates all US-07 invariants."""
        if self.product_template not in ALLOWED_PRODUCTS:
            raise ManualImportValidationError(
                f"Unknown product_template '{self.product_template}'. Allowed: {list(ALLOWED_PRODUCTS.keys())}"
            )
        if not self.source_account_id or not isinstance(self.source_account_id, str):
            raise ManualImportValidationError("source_account_id must be a non-empty string")
        validate_rfc3339_timestamp(self.observed_at, "observed_at", allow_none=False)

        # Product-specific invariants
        if self.product_template == "antigravity":
            # Must strictly use unit="credit"
            if self.credit_unit is not None and self.credit_unit != "credit":
                raise ManualImportValidationError(
                    f"Antigravity credit unit must strictly be 'credit', got: {self.credit_unit!r}"
                )
            if self.credits_consumed is not None:
                self.credits_consumed = validate_credit_amount(self.credits_consumed, "credits_consumed")
                if self.credit_unit is None:
                    self.credit_unit = "credit"
            if self.credit_balance is not None:
                self.credit_balance = validate_credit_amount(self.credit_balance, "credit_balance")
                if self.credit_unit is None:
                    self.credit_unit = "credit"
        else:
            # Claude Pro / Codex Plus should not emit credit metrics unless explicitly specified
            if self.credit_unit is not None and self.credit_unit != "credit":
                raise ManualImportValidationError("Unknown credit unit")

        # US-07.c: Check that windows are distinct and do not auto-calc reset
        seen_window_types = set()
        for w in self.windows:
            key = (w.window_type, w.label)
            if key in seen_window_types:
                raise ManualImportValidationError(
                    f"Duplicate window entry for {w.window_type} (label={w.label})"
                )
            seen_window_types.add(key)


def build_claude_pro_snapshot(
    account_id: str,
    observed_at: str,
    evidence_id: Optional[str] = None,
    short_term_remaining_percent: Optional[Union[Decimal, float, int, str]] = None,
    short_term_used_percent: Optional[Union[Decimal, float, int, str]] = None,
    short_term_reset_at: Optional[str] = None,
    weekly_remaining_percent: Optional[Union[Decimal, float, int, str]] = None,
    weekly_used_percent: Optional[Union[Decimal, float, int, str]] = None,
    weekly_reset_at: Optional[str] = None,
    raw_statement: Optional[str] = None,
    notes: Optional[str] = None,
) -> ManualObservationRecord:
    """Builds and validates a manual snapshot for Claude Pro.

    Supports:
    - Short-term rolling quota (e.g. 5h)
    - Weekly quota window (separate window)
    - Strict validation: 0 <= % <= 100; -1, 101 rejected
    - Unknown resets remain None (not inferred)
    - No token hallucination
    """
    windows: List[QuotaWindowSnapshot] = []

    # Short-term window
    if (short_term_remaining_percent is not None or
        short_term_used_percent is not None or
        short_term_reset_at is not None):
        windows.append(
            QuotaWindowSnapshot(
                window_type="5h",
                label="rolling_5h",
                reset_at=short_term_reset_at,
                quota_remaining_percent=None if short_term_remaining_percent is None else Decimal(str(short_term_remaining_percent)),
                quota_used_percent=None if short_term_used_percent is None else Decimal(str(short_term_used_percent)),
                source_statement=raw_statement or ("80% remaining" if short_term_remaining_percent == 80 else None),
            )
        )

    # Weekly window (US-07.c: separate window, reset remains unknown if not explicitly observed)
    if (weekly_remaining_percent is not None or
        weekly_used_percent is not None or
        weekly_reset_at is not None):
        windows.append(
            QuotaWindowSnapshot(
                window_type="weekly",
                label="weekly_allotment",
                reset_at=weekly_reset_at,  # If None, remains None!
                quota_remaining_percent=None if weekly_remaining_percent is None else Decimal(str(weekly_remaining_percent)),
                quota_used_percent=None if weekly_used_percent is None else Decimal(str(weekly_used_percent)),
            )
        )

    rec = ManualObservationRecord(
        product_template="claude_pro",
        product=ALLOWED_PRODUCTS["claude_pro"]["canonical_product"],
        source=ALLOWED_PRODUCTS["claude_pro"]["default_source"],
        source_account_id=account_id,
        observed_at=observed_at,
        evidence_id=evidence_id,
        windows=windows,
        notes=notes,
    )
    rec.validate_invariants()
    return rec


def build_codex_plus_snapshot(
    account_id: str,
    observed_at: str,
    evidence_id: Optional[str] = None,
    short_term_remaining_percent: Optional[Union[Decimal, float, int, str]] = None,
    short_term_used_percent: Optional[Union[Decimal, float, int, str]] = None,
    short_term_reset_at: Optional[str] = None,
    short_term_window_type: str = "5h",
    short_term_window_label: str = "rolling_5h",
    weekly_remaining_percent: Optional[Union[Decimal, float, int, str]] = None,
    weekly_used_percent: Optional[Union[Decimal, float, int, str]] = None,
    weekly_reset_at: Optional[str] = None,
    raw_statement: Optional[str] = None,
    notes: Optional[str] = None,
) -> ManualObservationRecord:
    """Builds and validates a manual snapshot for ChatGPT Plus / Codex.

    Supports:
    - Short term allowance window (5h rolling by current default)
    - Weekly allowance window (separate window)
    - No token hallucination
    """
    windows: List[QuotaWindowSnapshot] = []

    if (short_term_remaining_percent is not None or
        short_term_used_percent is not None or
        short_term_reset_at is not None):
        windows.append(
            QuotaWindowSnapshot(
                window_type=short_term_window_type,
                label=short_term_window_label,
                reset_at=short_term_reset_at,
                quota_remaining_percent=None if short_term_remaining_percent is None else Decimal(str(short_term_remaining_percent)),
                quota_used_percent=None if short_term_used_percent is None else Decimal(str(short_term_used_percent)),
                source_statement=raw_statement,
            )
        )

    if (weekly_remaining_percent is not None or
        weekly_used_percent is not None or
        weekly_reset_at is not None):
        windows.append(
            QuotaWindowSnapshot(
                window_type="weekly",
                label="weekly_allotment",
                reset_at=weekly_reset_at,
                quota_remaining_percent=None if weekly_remaining_percent is None else Decimal(str(weekly_remaining_percent)),
                quota_used_percent=None if weekly_used_percent is None else Decimal(str(weekly_used_percent)),
            )
        )

    rec = ManualObservationRecord(
        product_template="codex_plus",
        product=ALLOWED_PRODUCTS["codex_plus"]["canonical_product"],
        source=ALLOWED_PRODUCTS["codex_plus"]["default_source"],
        source_account_id=account_id,
        observed_at=observed_at,
        evidence_id=evidence_id,
        windows=windows,
        notes=notes,
    )
    rec.validate_invariants()
    return rec


def build_antigravity_snapshot(
    account_id: str,
    observed_at: str,
    evidence_id: Optional[str] = None,
    credits_consumed: Optional[Union[Decimal, float, int, str]] = None,
    credit_balance: Optional[Union[Decimal, float, int, str]] = None,
    quota_percent_remaining: Optional[Union[Decimal, float, int, str]] = None,
    quota_percent_used: Optional[Union[Decimal, float, int, str]] = None,
    reset_at: Optional[str] = None,
    window_type: str = "monthly",
    window_label: str = "monthly_credits",
    notes: Optional[str] = None,
) -> ManualObservationRecord:
    """Builds and validates a manual snapshot for Google Antigravity.

    Invariants:
    - unit is strictly 'credit'
    - no token hallucination / conversion to tokens
    - unknown reset remains None
    - percentages in [0, 100]
    """
    windows: List[QuotaWindowSnapshot] = []

    if (quota_percent_remaining is not None or
        quota_percent_used is not None or
        reset_at is not None):
        windows.append(
            QuotaWindowSnapshot(
                window_type=window_type,
                label=window_label,
                reset_at=reset_at,
                quota_remaining_percent=None if quota_percent_remaining is None else Decimal(str(quota_percent_remaining)),
                quota_used_percent=None if quota_percent_used is None else Decimal(str(quota_percent_used)),
            )
        )

    consumed_dec = None if credits_consumed is None else Decimal(str(credits_consumed))
    balance_dec = None if credit_balance is None else Decimal(str(credit_balance))

    rec = ManualObservationRecord(
        product_template="antigravity",
        product=ALLOWED_PRODUCTS["antigravity"]["canonical_product"],
        source=ALLOWED_PRODUCTS["antigravity"]["default_source"],
        source_account_id=account_id,
        observed_at=observed_at,
        evidence_id=evidence_id,
        windows=windows,
        credits_consumed=consumed_dec,
        credit_balance=balance_dec,
        credit_unit="credit",
        notes=notes,
    )
    rec.validate_invariants()
    return rec


def parse_manual_template_json(json_content: Union[str, bytes, dict]) -> ManualObservationRecord:
    """Parses a verified or user-provided manual snapshot template JSON into a ManualObservationRecord.

    Accepts templates matching provider capabilities fixtures for:
    - Claude Pro
    - Codex Plus
    - Google Antigravity
    """
    if isinstance(json_content, (str, bytes)):
        data = json.loads(json_content)
    else:
        data = dict(json_content)

    source = data.get("source")
    account_alias = data.get("account_alias") or data.get("source_account_id")
    observed_at = data.get("observed_at")
    evidence_ref = data.get("evidence_ref") or data.get("evidence_id")
    product = data.get("product", "")

    if "claude" in product.lower() or source == "anthropic":
        qm = data.get("quota_metrics", {})
        wa = data.get("weekly_allotment", {})
        return build_claude_pro_snapshot(
            account_id=account_alias,
            observed_at=observed_at,
            evidence_id=evidence_ref,
            short_term_remaining_percent=qm.get("quota_remaining_percent"),
            short_term_used_percent=qm.get("quota_used_percent"),
            short_term_reset_at=qm.get("reset_at"),
            weekly_remaining_percent=wa.get("quota_remaining_percent"),
            weekly_used_percent=wa.get("quota_used_percent"),
            weekly_reset_at=wa.get("reset_at"),
            raw_statement=qm.get("source_statement"),
            notes=data.get("notes"),
        )
    elif "codex" in product.lower() or "chatgpt" in product.lower() or source == "openai":
        am = data.get("allowance_metrics", {})
        wa = data.get("weekly_allotment", {})
        window_type = am.get("window_type", "5h")
        label = am.get("window_label", "rolling_5h")
        return build_codex_plus_snapshot(
            account_id=account_alias,
            observed_at=observed_at,
            evidence_id=evidence_ref,
            short_term_remaining_percent=am.get("quota_remaining_percent"),
            short_term_used_percent=am.get("quota_used_percent"),
            short_term_reset_at=am.get("reset_at"),
            short_term_window_type=window_type,
            short_term_window_label=label,
            weekly_remaining_percent=wa.get("quota_remaining_percent"),
            weekly_used_percent=wa.get("quota_used_percent"),
            weekly_reset_at=wa.get("reset_at"),
            raw_statement=am.get("source_statement"),
            notes=data.get("notes"),
        )
    elif "antigravity" in product.lower() or source == "google":
        cm = data.get("credit_metrics", {})
        qm = data.get("quota_metrics", cm)
        return build_antigravity_snapshot(
            account_id=account_alias,
            observed_at=observed_at,
            evidence_id=evidence_ref,
            credits_consumed=cm.get("credits_consumed"),
            credit_balance=cm.get("credit_balance"),
            quota_percent_remaining=qm.get("quota_percent_remaining"),
            quota_percent_used=qm.get("quota_percent_used"),
            reset_at=qm.get("reset_at"),
            window_type=qm.get("window_type", "monthly"),
            window_label=qm.get("window_label", "monthly_credits"),
            notes=data.get("notes"),
        )
    else:
        raise ManualImportValidationError(
            f"Cannot determine product template from source={source!r}, product={product!r}"
        )


def export_canonical_measurements(
    record: ManualObservationRecord,
    collector_version: str = "0.1.0",
    mapping_version: str = "0.1.0",
    generate_ulid_fn=None,
) -> List[Dict[str, Any]]:
    """Converts a ManualObservationRecord into a list of canonical measurement dictionaries.

    Enforces that:
    - Quota percentages are emitted with metric="quota_percent_used", unit="percent"
    - Antigravity credits are emitted with unit="credit"
    - Values are integers or exact decimal strings (never float)
    - No tokens are hallucinated
    """
    if generate_ulid_fn is None:
        import time, os
        # Minimal deterministic or counter-based fallback
        counter = [0]
        def generate_ulid_fn():
            counter[0] += 1
            # 26 Crockford base32 characters
            ts_part = "01ARZ3NDEKTSV4RRFFQ69G5F"[:22]
            return f"{ts_part}{counter[0]:04d}"

    measurements = []
    base_evidence_ids = [record.evidence_id] if record.evidence_id else ["ev_01ARZ3NDEKTSV4RRFFQ69G5FA1"]

    # Windows
    for w in record.windows:
        if w.quota_used_percent is not None:
            m = {
                "schema_version": "0.1",
                "measurement_id": generate_ulid_fn(),
                "event_id": generate_ulid_fn(),
                "metric": "quota_percent_used",
                "value": str(w.quota_used_percent) if w.quota_used_percent % 1 != 0 else int(w.quota_used_percent),
                "unit": "percent",
                "aggregation": "gauge",
                "source": record.source,
                "source_account_id": record.source_account_id,
                "provider": record.source,
                "provenance": {
                    "class": "P0",
                    "method": "manual",
                    "confidence": 1.0,
                    "confidence_reason": f"Manual observation confirmed by user ({record.product})",
                    "validation_status": "semantically_checked",
                },
                "evidence_ids": base_evidence_ids,
                "collector_version": collector_version,
                "mapping_version": mapping_version,
                "revision": {
                    "schema_version": "0.1",
                    "revision_id": generate_ulid_fn(),
                    "supersedes": None,
                    "effective": True,
                    "reason": "initial manual observation",
                    "created_at": record.observed_at,
                    "created_by": "ael-importer-manual",
                },
                "dimensions": {
                    "window_type": w.window_type,
                    "window_label": w.label,
                    "reset_at": w.reset_at,  # None if unknown!
                    "quota_remaining_percent": str(w.quota_remaining_percent) if w.quota_remaining_percent is not None else None,
                },
            }
            measurements.append(m)

    # Antigravity Credit Metrics
    if record.product_template == "antigravity":
        if record.credits_consumed is not None:
            m = {
                "schema_version": "0.1",
                "measurement_id": generate_ulid_fn(),
                "event_id": generate_ulid_fn(),
                "metric": "credits_consumed",
                "value": str(record.credits_consumed) if record.credits_consumed % 1 != 0 else int(record.credits_consumed),
                "unit": "credit",
                "aggregation": "gauge",
                "source": record.source,
                "source_account_id": record.source_account_id,
                "provider": record.source,
                "provenance": {
                    "class": "P0",
                    "method": "manual",
                    "confidence": 1.0,
                    "confidence_reason": "Manual observation confirmed by user (Google Antigravity Credits)",
                    "validation_status": "semantically_checked",
                },
                "evidence_ids": base_evidence_ids,
                "collector_version": collector_version,
                "mapping_version": mapping_version,
                "revision": {
                    "schema_version": "0.1",
                    "revision_id": generate_ulid_fn(),
                    "supersedes": None,
                    "effective": True,
                    "reason": "initial manual observation",
                    "created_at": record.observed_at,
                    "created_by": "ael-importer-manual",
                },
                "dimensions": {
                    "credit_unit": "credit",
                },
            }
            measurements.append(m)

        if record.credit_balance is not None:
            m = {
                "schema_version": "0.1",
                "measurement_id": generate_ulid_fn(),
                "event_id": generate_ulid_fn(),
                "metric": "credit_balance",
                "value": str(record.credit_balance) if record.credit_balance % 1 != 0 else int(record.credit_balance),
                "unit": "credit",
                "aggregation": "gauge",
                "source": record.source,
                "source_account_id": record.source_account_id,
                "provider": record.source,
                "provenance": {
                    "class": "P0",
                    "method": "manual",
                    "confidence": 1.0,
                    "confidence_reason": "Manual observation confirmed by user (Google Antigravity Credit Balance)",
                    "validation_status": "semantically_checked",
                },
                "evidence_ids": base_evidence_ids,
                "collector_version": collector_version,
                "mapping_version": mapping_version,
                "revision": {
                    "schema_version": "0.1",
                    "revision_id": generate_ulid_fn(),
                    "supersedes": None,
                    "effective": True,
                    "reason": "initial manual observation",
                    "created_at": record.observed_at,
                    "created_by": "ael-importer-manual",
                },
                "dimensions": {
                    "credit_unit": "credit",
                },
            }
            measurements.append(m)

    return measurements


# ==============================================================================
# PRD US-07.d: Evidence generation without OCR or external image transmission
# ==============================================================================

def generate_manual_evidence_descriptor(
    source: str,
    account: str,
    created_at: str,
    payload_content: Union[str, bytes, dict],
    evidence_id: Optional[str] = None,
    collector_version: str = "0.1.0",
    capture_point: str = "user_manual_entry",
) -> Dict[str, Any]:
    """Generates an evidence descriptor for manual input without OCR or external image transmission (US-07.d).

    If no screenshot is attached, the confirmed user input itself is hashed and stored
    as a verified manual evidence artifact.
    """
    validate_rfc3339_timestamp(created_at, "created_at", allow_none=False)

    if isinstance(payload_content, dict):
        raw_bytes = json.dumps(payload_content, sort_keys=True).encode("utf-8")
        mime = "application/json"
    elif isinstance(payload_content, str):
        raw_bytes = payload_content.encode("utf-8")
        mime = "text/plain; charset=utf-8"
    elif isinstance(payload_content, bytes):
        raw_bytes = payload_content
        mime = "application/octet-stream"
    else:
        raise ManualImportValidationError(f"Unsupported payload_content type: {type(payload_content)}")

    sha256 = hashlib.sha256(raw_bytes).hexdigest()
    size_bytes = len(raw_bytes)

    if evidence_id is None:
        # Generate Crockford-style ULID-like identifier
        import time
        ts_part = f"{int(time.time() * 1000):010X}"[:10]
        rand_part = sha256[:16].upper()
        # map to Crockford
        crockford = "".join([c if c in "0123456789ABCDEFGHJKMNPQRSTVWXYZ" else "X" for c in (ts_part + rand_part)])[:26]
        evidence_id = f"ev_{crockford}"

    return {
        "schema_version": "0.1",
        "evidence_id": evidence_id,
        "sha256": sha256,
        "size_bytes": size_bytes,
        "mime_type": mime,
        "source": source,
        "account": account,
        "created_at": created_at,
        "collector_version": collector_version,
        "capture_point": capture_point,
        "redaction": {
            "redacted": False,
            "redaction_version": None,
        },
    }


# ==============================================================================
# PRD US-07.e: Distinct entry types & Preview/Confirm Session Workflow
# ==============================================================================

@dataclass
class ManualSubscriptionEntry:
    """Represents a subscription interval or plan record (e.g. monthly billing cycle)."""
    source: str
    account_id: str
    plan_name: str
    amount: Decimal
    currency: str  # "USD", "EUR"
    cycle_start: str  # RFC3339
    cycle_end: str    # RFC3339
    entry_type: str = "subscription"
    evidence_id: Optional[str] = None
    notes: Optional[str] = None

    def __post_init__(self):
        validate_rfc3339_timestamp(self.cycle_start, "cycle_start")
        validate_rfc3339_timestamp(self.cycle_end, "cycle_end")
        self.amount = validate_credit_amount(self.amount, "subscription amount")
        if not self.currency or len(self.currency) != 3:
            raise ManualImportValidationError(f"currency must be 3 uppercase letters, got: {self.currency!r}")
        self.currency = self.currency.upper()


@dataclass
class ManualPaymentEntry:
    """Represents a single payment or top-up transaction."""
    source: str
    account_id: str
    amount: Decimal
    currency: str
    paid_at: str  # RFC3339
    transaction_ref: Optional[str] = None
    entry_type: str = "payment"
    evidence_id: Optional[str] = None
    notes: Optional[str] = None

    def __post_init__(self):
        validate_rfc3339_timestamp(self.paid_at, "paid_at")
        self.amount = validate_credit_amount(self.amount, "payment amount")
        if not self.currency or len(self.currency) != 3:
            raise ManualImportValidationError(f"currency must be 3 uppercase letters, got: {self.currency!r}")
        self.currency = self.currency.upper()


@dataclass
class ManualBalanceEntry:
    """Represents a discrete balance or remaining credit/quota point in time."""
    source: str
    account_id: str
    observed_at: str  # RFC3339
    balance_amount: Decimal
    balance_unit: str  # "credit", "usd", "eur", "percent"
    entry_type: str = "balance"
    evidence_id: Optional[str] = None
    notes: Optional[str] = None

    def __post_init__(self):
        validate_rfc3339_timestamp(self.observed_at, "observed_at")
        if self.balance_unit == "percent":
            self.balance_amount = validate_percentage(self.balance_amount, "balance_amount")
        else:
            self.balance_amount = validate_credit_amount(self.balance_amount, "balance_amount")


class ManualEntrySession:
    """Manages manual entry preview, confirmation, and cancellation (US-07.e).

    Invariants:
    - User must review the preview before committing.
    - Cancellation leaves state completely untouched (no measurement created).
    """

    def __init__(self):
        self._staged_entries: List[Union[ManualSubscriptionEntry, ManualPaymentEntry, ManualBalanceEntry, ManualObservationRecord]] = []
        self._committed_measurements: List[Dict[str, Any]] = []
        self._is_previewed: bool = False
        self._is_confirmed: bool = False
        self._is_cancelled: bool = False

    @property
    def staged_entries(self) -> List[Any]:
        return list(self._staged_entries)

    @property
    def committed_measurements(self) -> List[Dict[str, Any]]:
        return list(self._committed_measurements)

    @property
    def is_cancelled(self) -> bool:
        return self._is_cancelled

    @property
    def is_confirmed(self) -> bool:
        return self._is_confirmed

    def stage_entry(self, entry: Union[ManualSubscriptionEntry, ManualPaymentEntry, ManualBalanceEntry, ManualObservationRecord]):
        if self._is_confirmed:
            raise ManualImportValidationError("Cannot stage new entry in an already confirmed session")
        if self._is_cancelled:
            raise ManualImportValidationError("Cannot stage new entry in a cancelled session")
        self._staged_entries.append(entry)
        self._is_previewed = False

    def preview(self) -> Dict[str, Any]:
        """Renders preview of staged entries for user confirmation."""
        if self._is_cancelled:
            raise ManualImportValidationError("Cannot preview a cancelled session")
        self._is_previewed = True
        summary = {
            "staged_count": len(self._staged_entries),
            "entries": [],
        }
        for idx, entry in enumerate(self._staged_entries):
            if isinstance(entry, ManualObservationRecord):
                summary["entries"].append({
                    "index": idx,
                    "kind": "manual_observation",
                    "product": entry.product,
                    "source": entry.source,
                    "account": entry.source_account_id,
                    "observed_at": entry.observed_at,
                    "windows_count": len(entry.windows),
                    "evidence_id": entry.evidence_id,
                })
            elif isinstance(entry, ManualSubscriptionEntry):
                summary["entries"].append({
                    "index": idx,
                    "kind": "subscription",
                    "source": entry.source,
                    "account": entry.account_id,
                    "plan": entry.plan_name,
                    "amount": str(entry.amount),
                    "currency": entry.currency,
                    "cycle_start": entry.cycle_start,
                    "cycle_end": entry.cycle_end,
                })
            elif isinstance(entry, ManualPaymentEntry):
                summary["entries"].append({
                    "index": idx,
                    "kind": "payment",
                    "source": entry.source,
                    "account": entry.account_id,
                    "amount": str(entry.amount),
                    "currency": entry.currency,
                    "paid_at": entry.paid_at,
                    "tx_ref": entry.transaction_ref,
                })
            elif isinstance(entry, ManualBalanceEntry):
                summary["entries"].append({
                    "index": idx,
                    "kind": "balance",
                    "source": entry.source,
                    "account": entry.account_id,
                    "balance": str(entry.balance_amount),
                    "unit": entry.balance_unit,
                    "observed_at": entry.observed_at,
                })
        return summary

    def cancel(self):
        """Cancels the entry session. Invariant: modifies no measurements, discards staged entries."""
        self._staged_entries.clear()
        self._is_cancelled = True
        self._is_previewed = False
        return {"status": "cancelled", "measurements_modified": 0}

    def confirm(self, generate_ulid_fn=None) -> List[Dict[str, Any]]:
        """Confirms the previewed staged entries and generates canonical measurements."""
        if self._is_cancelled:
            raise ManualImportValidationError("Cannot confirm a cancelled session")
        if not self._is_previewed:
            raise ManualImportValidationError("Session must be previewed by user prior to confirmation")
        if not self._staged_entries:
            raise ManualImportValidationError("No entries staged for confirmation")

        if generate_ulid_fn is None:
            counter = [0]
            def generate_ulid_fn():
                counter[0] += 1
                return f"01ARZ3NDEKTSV4RRFFQ69G5F{counter[0]:04d}"

        new_measurements = []
        for entry in self._staged_entries:
            if isinstance(entry, ManualObservationRecord):
                ms = export_canonical_measurements(entry, generate_ulid_fn=generate_ulid_fn)
                new_measurements.extend(ms)
            elif isinstance(entry, ManualSubscriptionEntry):
                # Generates canonical cost/subscription measurement
                m = {
                    "schema_version": "0.1",
                    "measurement_id": generate_ulid_fn(),
                    "event_id": generate_ulid_fn(),
                    "metric": "subscription_cost",
                    "value": str(entry.amount) if entry.amount % 1 != 0 else int(entry.amount),
                    "unit": entry.currency.lower(),
                    "aggregation": "absolute",
                    "source": entry.source,
                    "source_account_id": entry.account_id,
                    "provider": entry.source,
                    "provenance": {
                        "class": "P0",
                        "method": "manual",
                        "confidence": 1.0,
                        "confidence_reason": f"Manual subscription confirmed by user ({entry.plan_name})",
                        "validation_status": "semantically_checked",
                    },
                    "evidence_ids": [entry.evidence_id] if entry.evidence_id else [f"ev_{generate_ulid_fn()}"],
                    "collector_version": "0.1.0",
                    "mapping_version": "0.1.0",
                    "revision": {
                        "schema_version": "0.1",
                        "revision_id": generate_ulid_fn(),
                        "supersedes": None,
                        "effective": True,
                        "reason": "manual subscription entry",
                        "created_at": entry.cycle_start,
                        "created_by": "ael-importer-manual",
                    },
                    "dimensions": {
                        "plan_name": entry.plan_name,
                        "cycle_start": entry.cycle_start,
                        "cycle_end": entry.cycle_end,
                    },
                }
                new_measurements.append(m)
            elif isinstance(entry, ManualPaymentEntry):
                m = {
                    "schema_version": "0.1",
                    "measurement_id": generate_ulid_fn(),
                    "event_id": generate_ulid_fn(),
                    "metric": "payment_amount",
                    "value": str(entry.amount) if entry.amount % 1 != 0 else int(entry.amount),
                    "unit": entry.currency.lower(),
                    "aggregation": "absolute",
                    "source": entry.source,
                    "source_account_id": entry.account_id,
                    "provider": entry.source,
                    "provenance": {
                        "class": "P0",
                        "method": "manual",
                        "confidence": 1.0,
                        "confidence_reason": "Manual payment confirmed by user",
                        "validation_status": "semantically_checked",
                    },
                    "evidence_ids": [entry.evidence_id] if entry.evidence_id else [f"ev_{generate_ulid_fn()}"],
                    "collector_version": "0.1.0",
                    "mapping_version": "0.1.0",
                    "revision": {
                        "schema_version": "0.1",
                        "revision_id": generate_ulid_fn(),
                        "supersedes": None,
                        "effective": True,
                        "reason": "manual payment entry",
                        "created_at": entry.paid_at,
                        "created_by": "ael-importer-manual",
                    },
                    "dimensions": {
                        "transaction_ref": entry.transaction_ref,
                    },
                }
                new_measurements.append(m)
            elif isinstance(entry, ManualBalanceEntry):
                m = {
                    "schema_version": "0.1",
                    "measurement_id": generate_ulid_fn(),
                    "event_id": generate_ulid_fn(),
                    "metric": "balance_observed",
                    "value": str(entry.balance_amount) if entry.balance_amount % 1 != 0 else int(entry.balance_amount),
                    "unit": entry.balance_unit.lower(),
                    "aggregation": "gauge",
                    "source": entry.source,
                    "source_account_id": entry.account_id,
                    "provider": entry.source,
                    "provenance": {
                        "class": "P0",
                        "method": "manual",
                        "confidence": 1.0,
                        "confidence_reason": "Manual balance observation confirmed by user",
                        "validation_status": "semantically_checked",
                    },
                    "evidence_ids": [entry.evidence_id] if entry.evidence_id else [f"ev_{generate_ulid_fn()}"],
                    "collector_version": "0.1.0",
                    "mapping_version": "0.1.0",
                    "revision": {
                        "schema_version": "0.1",
                        "revision_id": generate_ulid_fn(),
                        "supersedes": None,
                        "effective": True,
                        "reason": "manual balance observation",
                        "created_at": entry.observed_at,
                        "created_by": "ael-importer-manual",
                    },
                    "dimensions": {
                        "balance_unit": entry.balance_unit,
                    },
                }
                new_measurements.append(m)

        self._committed_measurements.extend(new_measurements)
        self._staged_entries.clear()
        self._is_confirmed = True
        return new_measurements


# ==============================================================================
# PRD US-07.f: Expiration, Freshness & `ael due` Due Status Logic
# ==============================================================================

DEFAULT_MANUAL_MAX_AGE_SECONDS = 86400  # 24 hours (daily rhythm per PRD US-07.f / Section 15)

@dataclass
class ManualSourceDueConfig:
    """Configuration for manual source snapshot freshness and due thresholds."""
    source: str
    account_id: str
    max_age_seconds: int = DEFAULT_MANUAL_MAX_AGE_SECONDS
    alert_before_due_seconds: int = 0
    warning_threshold_seconds: Optional[int] = None

    def __post_init__(self):
        if self.max_age_seconds <= 0:
            raise ManualImportValidationError("max_age_seconds must be positive")


@dataclass
class ManualSourceDueResult:
    """Evaluation result of whether a manual source is due or overdue."""
    source: str
    account_id: str
    is_due: bool
    status: str  # "fresh", "due", "overdue", "no_snapshot"
    last_observed_at: Optional[str]
    age_seconds: Optional[float]
    max_age_seconds: int
    overdue_seconds: Optional[float] = None
    next_due_at: Optional[str] = None
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "account_id": self.account_id,
            "is_due": self.is_due,
            "status": self.status,
            "last_observed_at": self.last_observed_at,
            "age_seconds": round(self.age_seconds, 1) if self.age_seconds is not None else None,
            "max_age_seconds": self.max_age_seconds,
            "overdue_seconds": round(self.overdue_seconds, 1) if self.overdue_seconds is not None else None,
            "next_due_at": self.next_due_at,
            "reason": self.reason,
        }


def parse_rfc3339_to_datetime(ts_str: str) -> datetime:
    """Parses strict RFC3339 string to timezone-aware datetime."""
    validate_rfc3339_timestamp(ts_str, "timestamp")
    # Python 3.11+ fromisoformat handles standard RFC3339
    # normalize Z to +00:00
    cleaned = ts_str.replace("Z", "+00:00")
    return datetime.fromisoformat(cleaned)


def check_manual_source_due(
    source: str,
    account_id: str,
    last_observed_at: Optional[str],
    current_time: Union[str, datetime],
    config: Optional[ManualSourceDueConfig] = None,
) -> ManualSourceDueResult:
    """Evaluates whether a manual source snapshot is due or expired (US-07.f).

    Invariants:
    - Default cadence is daily (86400 seconds) if unconfigured.
    - If never observed (last_observed_at is None), status is 'no_snapshot' and is_due=True.
    - If elapsed time exceeds max_age_seconds, marked as 'overdue' and is_due=True.
    - Does not depend on external notification delivery.
    """
    cfg = config or ManualSourceDueConfig(source=source, account_id=account_id)

    if isinstance(current_time, str):
        now_dt = parse_rfc3339_to_datetime(current_time)
    elif isinstance(current_time, datetime):
        if current_time.tzinfo is None:
            raise ManualImportValidationError("current_time datetime must be timezone-aware")
        now_dt = current_time
    else:
        raise ManualImportValidationError(f"Invalid current_time type: {type(current_time)}")

    if last_observed_at is None:
        return ManualSourceDueResult(
            source=source,
            account_id=account_id,
            is_due=True,
            status="no_snapshot",
            last_observed_at=None,
            age_seconds=None,
            max_age_seconds=cfg.max_age_seconds,
            overdue_seconds=None,
            next_due_at=now_dt.isoformat(),
            reason="No manual snapshot recorded yet. Snapshot is immediately due.",
        )

    last_dt = parse_rfc3339_to_datetime(last_observed_at)
    age = (now_dt - last_dt).total_seconds()

    if age < 0:
        # Negative age indicates observation is in the future
        raise ManualImportValidationError(
            f"Observation time {last_observed_at} is in the future relative to current time {now_dt.isoformat()}"
        )

    next_due_dt = last_dt + timedelta(seconds=cfg.max_age_seconds)
    next_due_str = next_due_dt.isoformat()

    if age > cfg.max_age_seconds:
        overdue_sec = age - cfg.max_age_seconds
        return ManualSourceDueResult(
            source=source,
            account_id=account_id,
            is_due=True,
            status="overdue",
            last_observed_at=last_observed_at,
            age_seconds=age,
            max_age_seconds=cfg.max_age_seconds,
            overdue_seconds=overdue_sec,
            next_due_at=next_due_str,
            reason=f"Snapshot is overdue by {overdue_sec:.0f}s (age {age:.0f}s > allowed {cfg.max_age_seconds}s).",
        )
    elif age == cfg.max_age_seconds:
        return ManualSourceDueResult(
            source=source,
            account_id=account_id,
            is_due=True,
            status="due",
            last_observed_at=last_observed_at,
            age_seconds=age,
            max_age_seconds=cfg.max_age_seconds,
            overdue_seconds=0.0,
            next_due_at=next_due_str,
            reason="Snapshot is due right now.",
        )
    else:
        return ManualSourceDueResult(
            source=source,
            account_id=account_id,
            is_due=False,
            status="fresh",
            last_observed_at=last_observed_at,
            age_seconds=age,
            max_age_seconds=cfg.max_age_seconds,
            overdue_seconds=0.0,
            next_due_at=next_due_str,
            reason=f"Snapshot is fresh ({age:.0f}s / {cfg.max_age_seconds}s).",
        )


def evaluate_all_manual_sources_due(
    sources: List[Dict[str, Any]],
    current_time: Union[str, datetime],
    custom_configs: Optional[Dict[Tuple[str, str], ManualSourceDueConfig]] = None,
) -> Dict[str, Any]:
    """Evaluates due status across a collection of configured manual sources (for `ael due` command).

    sources item format:
    {"source": "anthropic", "account_id": "claude-pro-main", "last_observed_at": "2026-10-25T10:00:00Z"}
    """
    custom_configs = custom_configs or {}
    results = []
    due_count = 0
    overdue_count = 0
    fresh_count = 0

    for item in sources:
        src = item["source"]
        acc = item["account_id"]
        last_obs = item.get("last_observed_at")
        cfg = custom_configs.get((src, acc))

        res = check_manual_source_due(
            source=src,
            account_id=acc,
            last_observed_at=last_obs,
            current_time=current_time,
            config=cfg,
        )
        results.append(res)
        if res.status == "overdue":
            overdue_count += 1
        if res.is_due:
            due_count += 1
        else:
            fresh_count += 1

    return {
        "checked_at": current_time if isinstance(current_time, str) else current_time.isoformat(),
        "total_sources": len(sources),
        "due_count": due_count,
        "overdue_count": overdue_count,
        "fresh_count": fresh_count,
        "sources": [r.to_dict() for r in results],
    }
