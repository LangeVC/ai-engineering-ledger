"""Persistent v0.4 collection jobs layered over the existing orchestrator."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from collectors.orchestration import CollectorOrchestrator, OrchestrationRunReport, utc_now_iso


CADENCE_SECONDS = {
    "hourly": 60 * 60,
    "daily": 24 * 60 * 60,
    "weekly": 7 * 24 * 60 * 60,
    "monthly": 30 * 24 * 60 * 60,
}


def _instant(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("collection job timestamps must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class CollectionJob:
    source_alias: str
    cadence: str = "manual"
    stale_after_seconds: int | None = None
    enabled: bool = True
    checkpoint: dict[str, Any] = field(default_factory=dict)
    last_run_id: str | None = None
    revision: int = 1
    job_id: str = ""

    def __post_init__(self) -> None:
        if not self.source_alias:
            raise ValueError("source_alias is required")
        if self.cadence not in {"manual", *CADENCE_SECONDS}:
            raise ValueError(f"unsupported cadence: {self.cadence}")
        if self.stale_after_seconds is not None and self.stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        if self.revision < 1:
            raise ValueError("revision must be positive")
        if not self.job_id:
            digest = hashlib.sha256(self.source_alias.encode()).hexdigest()[:20]
            object.__setattr__(self, "job_id", f"job_{digest}")

    def to_contract(self) -> dict[str, Any]:
        return {
            "schema_version": "0.4",
            "job_id": self.job_id,
            "source_alias": self.source_alias,
            "cadence": self.cadence,
            "stale_after_seconds": self.stale_after_seconds,
            "enabled": self.enabled,
            "checkpoint": self.checkpoint,
            "last_run_id": self.last_run_id,
            "revision": self.revision,
        }


class CollectionJobStore:
    """SQLite persistence for jobs and their terminal orchestration runs."""

    def __init__(self, database: str | Path):
        self.database = Path(database)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def save(self, job: CollectionJob, *, updated_at: str | None = None) -> CollectionJob:
        stamp = updated_at or utc_now_iso()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT revision FROM collection_jobs WHERE source_alias = ?", (job.source_alias,)
            ).fetchone()
            revision = max(job.revision, int(existing["revision"]) + 1) if existing else job.revision
            stored = CollectionJob(
                job_id=job.job_id,
                source_alias=job.source_alias,
                cadence=job.cadence,
                stale_after_seconds=job.stale_after_seconds,
                enabled=job.enabled,
                checkpoint=dict(job.checkpoint),
                last_run_id=job.last_run_id,
                revision=revision,
            )
            connection.execute(
                """INSERT INTO collection_jobs (
                     job_id, source_alias, cadence, stale_after_seconds, enabled,
                     checkpoint_json, last_run_id, revision, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source_alias) DO UPDATE SET
                     cadence=excluded.cadence,
                     stale_after_seconds=excluded.stale_after_seconds,
                     enabled=excluded.enabled,
                     checkpoint_json=excluded.checkpoint_json,
                     last_run_id=excluded.last_run_id,
                     revision=excluded.revision,
                     updated_at=excluded.updated_at""",
                (
                    stored.job_id,
                    stored.source_alias,
                    stored.cadence,
                    stored.stale_after_seconds,
                    1 if stored.enabled else 0,
                    json.dumps(stored.checkpoint, sort_keys=True, separators=(",", ":")),
                    stored.last_run_id,
                    stored.revision,
                    stamp,
                ),
            )
        return stored

    def list(self) -> list[CollectionJob]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM collection_jobs ORDER BY source_alias").fetchall()
        return [self._job(row) for row in rows]

    def get(self, source_alias: str) -> CollectionJob | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM collection_jobs WHERE source_alias = ?", (source_alias,)
            ).fetchone()
        return self._job(row) if row else None

    @staticmethod
    def _job(row: sqlite3.Row) -> CollectionJob:
        return CollectionJob(
            job_id=row["job_id"],
            source_alias=row["source_alias"],
            cadence=row["cadence"],
            stale_after_seconds=row["stale_after_seconds"],
            enabled=bool(row["enabled"]),
            checkpoint=json.loads(row["checkpoint_json"]),
            last_run_id=row["last_run_id"],
            revision=int(row["revision"]),
        )

    def last_terminal_run(self, job_id: str) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                """SELECT * FROM collection_runs
                   WHERE job_id = ? AND status IN ('success','partial','failed','manual_due','skipped_locked')
                   ORDER BY started_at DESC, run_id DESC LIMIT 1""",
                (job_id,),
            ).fetchone()

    def is_due(self, job: CollectionJob, *, now: str | datetime) -> bool:
        if not job.enabled:
            return False
        if job.cadence == "manual":
            return True
        last = self.last_terminal_run(job.job_id)
        if last is None:
            return True
        return _instant(now) >= _instant(last["started_at"]) + timedelta(seconds=CADENCE_SECONDS[job.cadence])

    def due(self, *, now: str | datetime) -> list[CollectionJob]:
        return [job for job in self.list() if self.is_due(job, now=now)]

    def record_report(self, job: CollectionJob, report: OrchestrationRunReport) -> str:
        cli = report.to_cli_result()
        run_status = {
            "success": "success",
            "partial": "partial",
            "failure": "failed",
        }[cli["status"]]
        if cli["runs"] and all(item["status"] == "skipped_locked" for item in cli["runs"]):
            run_status = "skipped_locked"
        if cli["runs"] and all(item["status"] == "skipped_due_manual" for item in cli["runs"]):
            run_status = "manual_due"
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO collection_runs (
                     run_id, job_id, trigger, status, started_at, finished_at, result_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    report.run_id,
                    job.job_id,
                    report.trigger,
                    run_status,
                    report.started_at,
                    report.finished_at,
                    json.dumps(cli, sort_keys=True, separators=(",", ":")),
                ),
            )
            connection.execute(
                "UPDATE collection_jobs SET last_run_id = ?, updated_at = ? WHERE job_id = ?",
                (report.run_id, report.finished_at, job.job_id),
            )
        return run_status

    def record_health(self, health: Any) -> None:
        contract = health.to_contract()
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO source_health (
                     health_id, source_alias, state, reason_code, rule_version,
                     evaluated_at, last_attempt_at, last_success_at, details_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    health.health_id,
                    contract["source_alias"],
                    contract["state"],
                    contract["reason_code"],
                    contract["rule_version"],
                    contract["evaluated_at"],
                    contract["last_attempt_at"],
                    contract["last_success_at"],
                    json.dumps(contract["details"], sort_keys=True, separators=(",", ":")),
                ),
            )


class PersistentCollectionRunner:
    """Select persisted due jobs and execute them through CollectorOrchestrator."""

    def __init__(self, store: CollectionJobStore, orchestrator: CollectorOrchestrator):
        self.store = store
        self.orchestrator = orchestrator

    def run_due(self, *, trigger: str, now: str | datetime) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        registered = {task.account_alias: task for task in self.orchestrator._registered_tasks}
        for job in self.store.due(now=now):
            task = registered.get(job.source_alias)
            if task is None:
                from derive.health import SourceHealth
                evaluated = _iso(_instant(now))
                self.store.record_health(SourceHealth(
                    source_alias=job.source_alias,
                    state="failed",
                    reason_code="adapter_not_registered",
                    evaluated_at=evaluated,
                ))
                results.append(
                    {
                        "job_id": job.job_id,
                        "source_alias": job.source_alias,
                        "status": "failed",
                        "reason_code": "adapter_not_registered",
                    }
                )
                continue
            report = self.orchestrator.run(trigger=trigger, account_alias=task.account_alias)
            status = self.store.record_report(job, report)
            from derive.health import derive_source_health
            self.store.record_health(derive_source_health(
                job,
                evaluated_at=report.finished_at,
                last_attempt_at=report.finished_at,
                last_success_at=report.finished_at if status == "success" else None,
                last_status=status,
            ))
            results.append(
                {
                    "job_id": job.job_id,
                    "source_alias": job.source_alias,
                    "run_id": report.run_id,
                    "status": status,
                }
            )
        return results

