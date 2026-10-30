from __future__ import annotations

import pytest

from collectors.orchestration.jobs import CollectionJob
from derive.health import derive_source_health


def state(job: CollectionJob, **values: object) -> str:
    return derive_source_health(job, evaluated_at="2026-10-30T12:00:00Z", **values).state


def test_manual_and_unknown_never_become_healthy() -> None:
    assert state(CollectionJob("claude", cadence="manual")) == "manual_due"
    assert state(CollectionJob("off", cadence="daily", enabled=False)) == "unknown"


@pytest.mark.parametrize("capability", ["unsupported", "unauthorized"])
def test_capability_state_is_explicit(capability: str) -> None:
    result = derive_source_health(
        CollectionJob("source", cadence="daily"),
        evaluated_at="2026-10-30T12:00:00Z",
        capability_state=capability,
    )
    assert result.state == capability
    assert result.reason_code == f"capability_{capability}"


def test_failed_run_preserves_last_success_and_stale_is_deterministic() -> None:
    job = CollectionJob("deepseek", cadence="daily", stale_after_seconds=3600)
    result = derive_source_health(
        job,
        evaluated_at="2026-10-30T12:00:00Z",
        last_attempt_at="2026-10-30T11:30:00Z",
        last_success_at="2026-10-30T10:00:00Z",
        last_status="failed",
    )
    assert result.last_success_at == "2026-10-30T10:00:00Z"
    assert result.state == "failed"


def test_health_contract_contains_versioned_reason() -> None:
    result = derive_source_health(
        CollectionJob("deepseek", cadence="daily"),
        evaluated_at="2026-10-30T12:00:00Z",
        last_success_at="2026-10-30T11:00:00Z",
        last_status="success",
    ).to_contract()
    assert result["schema_version"] == "0.4"
    assert result["state"] == "healthy"
    assert result["reason_code"] == "within_cadence"

