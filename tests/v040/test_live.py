from __future__ import annotations

from pathlib import Path
import sqlite3

from ael.ledger import OperationalLedger
from collectors.orchestration.jobs import CollectionJob, CollectionJobStore


def store(tmp_path: Path) -> CollectionJobStore:
    database = tmp_path / "ael.db"
    OperationalLedger(database).initialize()
    return CollectionJobStore(database)


def test_jobs_are_stable_revisioned_and_due_deterministically(tmp_path: Path) -> None:
    jobs = store(tmp_path)
    first = jobs.save(CollectionJob("deepseek-main", cadence="daily"), updated_at="2026-10-30T00:00:00Z")
    second = jobs.save(CollectionJob("deepseek-main", cadence="daily"), updated_at="2026-10-30T01:00:00Z")
    assert first.job_id == second.job_id
    assert second.revision == 2
    assert jobs.due(now="2026-10-30T12:00:00Z") == [second]


def test_disabled_job_is_not_due_and_manual_job_is_due(tmp_path: Path) -> None:
    jobs = store(tmp_path)
    jobs.save(CollectionJob("claude-pro", cadence="manual"))
    jobs.save(CollectionJob("disabled", cadence="daily", enabled=False))
    assert [job.source_alias for job in jobs.due(now="2026-10-30T12:00:00Z")] == ["claude-pro"]


def test_missing_registered_adapter_is_explicit_failure(tmp_path: Path) -> None:
    from collectors.orchestration import CollectorOrchestrator
    from collectors.orchestration.jobs import PersistentCollectionRunner
    jobs = store(tmp_path)
    job = jobs.save(CollectionJob("missing", cadence="daily"))
    result = PersistentCollectionRunner(jobs, CollectorOrchestrator()).run_due(
        trigger="scheduler", now="2026-10-30T12:00:00Z"
    )
    assert result == [{"job_id": job.job_id, "source_alias": "missing", "status": "failed", "reason_code": "adapter_not_registered"}]
    with sqlite3.connect(jobs.database) as connection:
        assert connection.execute(
            "SELECT state, reason_code FROM source_health WHERE source_alias='missing'"
        ).fetchone() == ("failed", "adapter_not_registered")

