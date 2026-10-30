"""Deterministic source-health derivation for AEL living studies."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from collectors.orchestration.jobs import CADENCE_SECONDS, CollectionJob


RULE_VERSION = "0.4.0"


def _instant(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("health timestamps must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None


@dataclass(frozen=True)
class SourceHealth:
    source_alias: str
    state: str
    reason_code: str
    evaluated_at: str
    last_attempt_at: str | None = None
    last_success_at: str | None = None
    rule_version: str = RULE_VERSION
    details: dict[str, Any] | None = None

    @property
    def health_id(self) -> str:
        seed = "|".join(
            (
                self.source_alias,
                self.state,
                self.reason_code,
                self.rule_version,
                self.evaluated_at,
                self.last_attempt_at or "",
                self.last_success_at or "",
            )
        )
        return "health_" + hashlib.sha256(seed.encode()).hexdigest()[:24]

    def to_contract(self) -> dict[str, Any]:
        return {
            "schema_version": "0.4",
            "source_alias": self.source_alias,
            "state": self.state,
            "reason_code": self.reason_code,
            "rule_version": self.rule_version,
            "evaluated_at": self.evaluated_at,
            "last_attempt_at": self.last_attempt_at,
            "last_success_at": self.last_success_at,
            "details": dict(self.details or {}),
        }


def derive_source_health(
    job: CollectionJob,
    *,
    evaluated_at: str | datetime,
    last_attempt_at: str | datetime | None = None,
    last_success_at: str | datetime | None = None,
    last_status: str | None = None,
    coverage_state: str = "complete",
    capability_state: str = "ready",
) -> SourceHealth:
    """Derive one explicit health state without treating absence as zero/healthy."""

    now = _instant(evaluated_at)
    attempt = _instant(last_attempt_at)
    success = _instant(last_success_at)
    assert now is not None
    common = {
        "source_alias": job.source_alias,
        "evaluated_at": _iso(now),
        "last_attempt_at": _iso(attempt),
        "last_success_at": _iso(success),
        "details": {
            "cadence": job.cadence,
            "stale_after_seconds": job.stale_after_seconds,
            "coverage_state": coverage_state,
            "capability_state": capability_state,
        },
    }

    if capability_state in {"unsupported", "unauthorized"}:
        return SourceHealth(
            state=capability_state,
            reason_code=f"capability_{capability_state}",
            **common,
        )
    if not job.enabled:
        return SourceHealth(state="unknown", reason_code="job_disabled", **common)
    if job.cadence == "manual":
        return SourceHealth(state="manual_due", reason_code="manual_collection_required", **common)
    if job.cadence not in CADENCE_SECONDS:
        return SourceHealth(state="unknown", reason_code="cadence_unknown", **common)
    if coverage_state in {"partial", "unknown", "unverified", "unavailable"}:
        return SourceHealth(state="partial", reason_code=f"coverage_{coverage_state}", **common)
    if last_status in {"failed", "skipped_locked"} and (success is None or attempt and attempt >= success):
        return SourceHealth(state="failed", reason_code=f"last_run_{last_status}", **common)
    if success is None:
        return SourceHealth(state="due", reason_code="never_succeeded", **common)

    stale_after = job.stale_after_seconds
    if stale_after is not None and now >= success + timedelta(seconds=stale_after):
        return SourceHealth(state="stale", reason_code="last_success_outside_stale_threshold", **common)

    due_after = success + timedelta(seconds=CADENCE_SECONDS[job.cadence])
    if now >= due_after:
        return SourceHealth(state="due", reason_code="cadence_elapsed", **common)
    return SourceHealth(state="healthy", reason_code="within_cadence", **common)

