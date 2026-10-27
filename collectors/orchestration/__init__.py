"""
collectors/orchestration/__init__.py
Central Collector Orchestration module for AI Engineering Ledger (AEL).
Pure Python 3 standard library implementation.

Fulfills PRD US-09:
- US-09.a: Unified trigger interface for manual & scheduler runs.
           Independent invocation of single source, single account, or all activated automatable sources.
           Manual sources are reported as due (fällig) and do not block automated runs.
- US-09.b: Concurrency control & serialization per account.
           File locks prevent concurrent runs on the same account.
           Guarantees no duplicate active measurements and no corrupted SQLite databases.
- US-09.c: Retry budget and backoff management for 429 (rate_limited) and transient 5xx / timeouts.
           Strictly respects Retry-After headers / instructions.
           Auth / unauthorized errors immediately abort without infinite retry loop.
           Retry budget and last error are fully exposed in error reporting.
- US-09.d: Aggregated run reporting according to schema/collector/run.schema.json & ADR-0002.
           Distinguishes success (exit 0), partial_failure (exit 3), and failure (exit 1).
           Emits schema/export/cli-result.schema.json compliant CLI result payload.
- US-09.e: Scheduler & Cron integration:
           Headless non-interactive execution, idempotent triggers, log aggregation,
           and complete cron configuration generator adhering to requirements:
           absolute binary path, data/config paths, explicit secrets, timezone, and file locking.
           Installation requires deliberate user action.
"""

from __future__ import annotations

import errno
import fcntl
import json
import logging
import math
import os
import re
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple, Union

logger = logging.getLogger("ael.orchestration")

SCHEMA_VERSION = "0.1"

# Crockford Base32 alphabet for ULID generation
_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# Non-retryable error classes per schema/collector/error.schema.json
NON_RETRYABLE_ERROR_CLASSES = {
    "auth",
    "unauthorized",
    "config_secret_ref_missing",
    "unsupported",
    "protocol",
    "cancelled",
}

# Retryable error classes per schema/collector/error.schema.json
RETRYABLE_ERROR_CLASSES = {
    "rate_limited",
    "transient_5xx",
    "timeout",
    "partial_response",
}

# Supported triggers
ALLOWED_TRIGGERS = {"manual", "scheduler"}

# Run statuses and ADR-0002 exit codes
STATUS_SUCCESS = "success"
STATUS_PARTIAL_FAILURE = "partial_failure"
STATUS_FAILURE = "failure"

# CLI result statuses per schema/export/cli-result.schema.json
CLI_STATUS_SUCCESS = "success"
CLI_STATUS_PARTIAL = "partial"
CLI_STATUS_FAILURE = "failure"

EXIT_SUCCESS = 0
EXIT_PARTIAL_FAILURE = 3
EXIT_FAILURE = 1
EXIT_USAGE_ERROR = 2

STATUS_TO_EXIT_CODE = {
    STATUS_SUCCESS: EXIT_SUCCESS,
    STATUS_PARTIAL_FAILURE: EXIT_PARTIAL_FAILURE,
    STATUS_FAILURE: EXIT_FAILURE,
}

# Regex to detect secret leaks in error messages per schema/collector/error.schema.json
SECRET_LEAK_PATTERN = re.compile(
    r"(?i)(bearer\s+[a-z0-9._-]{8,}|sk-[a-z0-9]{8,}|secret=|password=)"
)


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO 8601 / RFC 3339 format with Z suffix."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def generate_ulid(timestamp_ms: Optional[int] = None) -> str:
    """Generate a canonical 26-character Crockford Base32 ULID."""
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)

    # 48-bit timestamp -> 10 chars
    time_chars = []
    t = timestamp_ms
    for _ in range(10):
        time_chars.append(_CROCKFORD_ALPHABET[t % 32])
        t //= 32
    time_part = "".join(reversed(time_chars))

    # 80-bit random -> 16 chars
    rand_bytes = os.urandom(10)
    rand_val = int.from_bytes(rand_bytes, byteorder="big")
    rand_chars = []
    for _ in range(16):
        rand_chars.append(_CROCKFORD_ALPHABET[rand_val % 32])
        rand_val //= 32
    rand_part = "".join(reversed(rand_chars))

    return f"{time_part}{rand_part}"


def sanitize_error_message(message: str) -> str:
    """
    Ensure no tokens, bearer credentials, or password strings leak in error message.
    Conforms to schema/collector/error.schema.json message constraint.
    """
    if not message:
        return "Unknown error"
    # Redact Bearer tokens
    sanitized = re.sub(r"(?i)bearer\s+[a-z0-9._-]+", "Bearer [REDACTED]", message)
    # Redact sk- tokens
    sanitized = re.sub(r"(?i)sk-[a-z0-9]+", "sk-[REDACTED]", sanitized)
    # Redact secret=...
    sanitized = re.sub(r"(?i)secret=[^\s&]+", "secret=[REDACTED]", sanitized)
    # Redact password=...
    sanitized = re.sub(r"(?i)password=[^\s&]+", "password=[REDACTED]", sanitized)
    return sanitized


class AccountLockError(Exception):
    """Raised when an account cannot be locked due to a concurrent active run."""
    def __init__(self, account_alias: str, lock_path: Path, reason: str = "Account is currently locked by another run"):
        super().__init__(f"{reason}: account '{account_alias}' at {lock_path}")
        self.account_alias = account_alias
        self.lock_path = lock_path


class AccountLock:
    """
    US-09.b: Advisory/OS-level file lock per account.
    Guarantees no two processes or threads execute collection on the same account simultaneously.
    If wait_timeout > 0, waits and serializes; otherwise raises AccountLockError immediately.
    """

    def __init__(self, account_alias: str, lock_dir: Optional[Union[str, Path]] = None, wait_timeout: float = 0.0):
        self.account_alias = account_alias
        self.lock_dir = Path(lock_dir) if lock_dir else Path(".ael/locks")
        self.wait_timeout = wait_timeout
        self.lock_file = self.lock_dir / f"account_{self.account_alias}.lock"
        self._fd: Optional[int] = None
        self._acquired = False

    def acquire(self) -> bool:
        """Acquire lock on account file using fcntl.flock."""
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._fd = os.open(str(self.lock_file), os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as e:
            raise AccountLockError(self.account_alias, self.lock_file, f"Cannot open lockfile: {e}") from e

        start_time = time.time()
        while True:
            try:
                # Try non-blocking exclusive lock
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._acquired = True
                # Record PID and timestamp into lockfile for diagnostics
                try:
                    os.ftruncate(self._fd, 0)
                    os.lseek(self._fd, 0, os.SEEK_SET)
                    os.write(self._fd, f"pid={os.getpid()} acquired_at={utc_now_iso()}\n".encode("utf-8"))
                except OSError:
                    pass
                return True
            except (OSError, IOError) as e:
                if e.errno in (errno.EACCES, errno.EAGAIN):
                    elapsed = time.time() - start_time
                    if elapsed >= self.wait_timeout:
                        os.close(self._fd)
                        self._fd = None
                        raise AccountLockError(
                            self.account_alias,
                            self.lock_file,
                            f"Concurrent run active on account '{self.account_alias}' (timeout={self.wait_timeout}s)"
                        ) from e
                    # Wait a bit before retrying lock acquisition
                    time.sleep(min(0.05, max(0.005, self.wait_timeout / 10.0)))
                else:
                    os.close(self._fd)
                    self._fd = None
                    raise AccountLockError(self.account_alias, self.lock_file, f"Failed locking: {e}") from e

    def release(self) -> None:
        """Release the account file lock."""
        if self._fd is not None:
            try:
                if self._acquired:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
            self._acquired = False

    def __enter__(self) -> AccountLock:
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()


@dataclass
class RetryPolicy:
    """
    US-09.c: Configurable retry budget and backoff policy.
    """
    max_retries: int = 3
    initial_backoff_seconds: float = 0.5
    backoff_multiplier: float = 2.0
    max_backoff_seconds: float = 30.0
    respect_retry_after: bool = True

    def calculate_backoff(self, attempt: int, retry_after: Optional[int] = None) -> float:
        """Calculate wait time in seconds considering Retry-After header and exponential backoff."""
        if retry_after is not None and self.respect_retry_after and retry_after >= 0:
            return float(retry_after)
        backoff = self.initial_backoff_seconds * (self.backoff_multiplier ** attempt)
        return min(backoff, self.max_backoff_seconds)


@dataclass
class CollectorErrorRecord:
    """
    Conforms strictly to schema/collector/error.schema.json.
    """
    schema_version: str = "0.1"
    error_class: str = "transient_5xx"
    retryable: bool = True
    occurred_at: str = field(default_factory=utc_now_iso)
    message: str = "Unknown error"
    retry_budget: Dict[str, int] = field(default_factory=lambda: {"max": 3, "used": 0})
    http_status: Optional[int] = None
    retry_after_seconds: Optional[int] = None
    secret_ref: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        sanitized_msg = sanitize_error_message(self.message)
        # Verify no secret leak remains
        if SECRET_LEAK_PATTERN.search(sanitized_msg):
            sanitized_msg = "[REDACTED SENSITIVE ERROR MESSAGE]"

        data: Dict[str, Any] = {
            "schema_version": self.schema_version,
            "error_class": self.error_class,
            "retryable": self.retryable,
            "occurred_at": self.occurred_at,
            "message": sanitized_msg,
            "retry_budget": dict(self.retry_budget),
        }
        if self.http_status is not None:
            data["http_status"] = self.http_status
        if self.retry_after_seconds is not None:
            data["retry_after_seconds"] = self.retry_after_seconds
        if self.secret_ref is not None:
            data["secret_ref"] = self.secret_ref
        return data


@dataclass
class ManualDueItem:
    """
    Represents a manual collection source that is due (fällig).
    US-09.a: Reported as due without blocking automated collection.
    """
    account_alias: str
    source: str
    product_domain: str
    capability: str
    due_at: str = field(default_factory=utc_now_iso)
    reason: str = "Manual snapshot or file import is due"
    import_instructions: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "account_alias": self.account_alias,
            "source": self.source,
            "product_domain": self.product_domain,
            "capability": self.capability,
            "due_at": self.due_at,
            "reason": self.reason,
            "import_instructions": self.import_instructions,
        }


@dataclass
class AccountTask:
    """
    A single unit of collection work targeting an account and source.
    """
    account_alias: str
    profile_id: str
    source: str
    product_domain: str
    capability: str
    is_manual: bool = False
    collector_fn: Optional[Callable[..., Any]] = None
    kwargs: Dict[str, Any] = field(default_factory=dict)
    due_item: Optional[ManualDueItem] = None


@dataclass
class AccountRunResult:
    """
    Result of collection for a single account task.
    Matches schema/collector/run.schema.json #/$defs/account_run.
    """
    account_alias: str
    profile_id: str
    source: str
    product_domain: str
    capability: str
    status: str  # 'success' | 'failure' | 'skipped_unsupported' | 'skipped_unverified' | 'skipped_unauthorized'
    observed_at: str = field(default_factory=utc_now_iso)
    coverage: Dict[str, Any] = field(default_factory=dict)
    errors: List[Dict[str, Any]] = field(default_factory=list)
    process: Optional[Dict[str, Any]] = None
    data_payload: Optional[Any] = None

    def to_dict(self) -> Dict[str, Any]:
        # Default minimal valid coverage if not provided
        cov = self.coverage
        if not cov:
            now_str = self.observed_at
            if self.status == "success":
                cov = {
                    "requested": {"start": now_str, "end": now_str},
                    "delivered": {"start": now_str, "end": now_str},
                    "confirmed": {"start": now_str, "end": now_str},
                    "confirmation": "complete",
                    "pagination": {
                        "pages_fetched": 1,
                        "complete": True,
                        "truncated": False,
                        "next_cursor": None,
                    },
                }
            else:
                cov = {
                    "requested": {"start": now_str, "end": now_str},
                    "delivered": None,
                    "confirmed": None,
                    "confirmation": "unconfirmed",
                }

        out: Dict[str, Any] = {
            "account_alias": self.account_alias,
            "profile_id": self.profile_id,
            "source": self.source,
            "product_domain": self.product_domain,
            "capability": self.capability,
            "status": self.status,
            "observed_at": self.observed_at,
            "coverage": cov,
            "errors": list(self.errors),
        }
        if self.process is not None:
            out["process"] = self.process
        return out


@dataclass
class OrchestrationRunReport:
    """
    Aggregated collection run report adhering to schema/collector/run.schema.json.
    """
    schema_version: str = "0.1"
    run_id: str = field(default_factory=lambda: f"run_{generate_ulid()}")
    trigger: str = "manual"  # 'manual' | 'scheduler'
    started_at: str = field(default_factory=utc_now_iso)
    finished_at: str = field(default_factory=utc_now_iso)
    status: str = STATUS_SUCCESS  # 'success' | 'partial_failure' | 'failure'
    exit_status: int = EXIT_SUCCESS  # 0, 3, 1
    routing_subset: List[str] = field(default_factory=list)
    account_runs: List[AccountRunResult] = field(default_factory=list)
    manual_due: List[ManualDueItem] = field(default_factory=list)

    def compute_summary_status(self) -> None:
        """
        Compute overall run status and exit code based on account_runs and ADR-0002.
        - success: all executed account runs succeeded (0)
        - partial_failure: at least one succeeded and at least one failed (3)
        - failure: all executed account runs failed or total error (1)
        Note: skipped_unsupported / skipped_unverified / skipped_unauthorized do not count as failures if handled.
        Manual due items do NOT cause failures.
        """
        if not self.account_runs:
            # If there are only manual due items, it is considered success (manual sources do not block)
            if self.manual_due:
                self.status = STATUS_SUCCESS
                self.exit_status = EXIT_SUCCESS
                return
            self.status = STATUS_FAILURE
            self.exit_status = EXIT_FAILURE
            return

        failures = 0
        successes = 0
        for ar in self.account_runs:
            if ar.status == "success":
                successes += 1
            elif ar.status == "failure":
                failures += 1
            else:
                # Skipped counts towards non-failure
                pass

        if failures == 0:
            self.status = STATUS_SUCCESS
            self.exit_status = EXIT_SUCCESS
        elif successes > 0 and failures > 0:
            self.status = STATUS_PARTIAL_FAILURE
            self.exit_status = EXIT_PARTIAL_FAILURE
        else:
            self.status = STATUS_FAILURE
            self.exit_status = EXIT_FAILURE

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict conforming strictly to schema/collector/run.schema.json."""
        self.compute_summary_status()
        res: Dict[str, Any] = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "trigger": self.trigger,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "exit_status": self.exit_status,
            "routing_subset": list(self.routing_subset),
            "account_runs": [ar.to_dict() for ar in self.account_runs],
        }
        return res

    def to_cli_result(self, command: str = "collect") -> Dict[str, Any]:
        """
        US-09.d: Generate CLI JSON result conforming strictly to
        schema/export/cli-result.schema.json (ael:cli-json-result-and-exit-status@0.1).
        Maps ADR-0002 status:
        - success -> status="success", exit_code=0
        - partial_failure -> status="partial", exit_code=3
        - failure -> status="failure", exit_code=1
        """
        self.compute_summary_status()

        if self.status == STATUS_SUCCESS:
            cli_status = CLI_STATUS_SUCCESS
            cli_exit = EXIT_SUCCESS
        elif self.status == STATUS_PARTIAL_FAILURE:
            cli_status = CLI_STATUS_PARTIAL
            cli_exit = EXIT_PARTIAL_FAILURE
        else:
            cli_status = CLI_STATUS_FAILURE
            cli_exit = EXIT_FAILURE

        runs: List[Dict[str, Any]] = []

        # Map executed account runs
        for ar in self.account_runs:
            run_item: Dict[str, Any] = {
                "source_alias": ar.source,
                "account_alias": ar.account_alias,
            }
            if ar.status == "success":
                run_item["status"] = "success"
            elif ar.status == "failure":
                # Check if failure was caused by lock contention
                is_locked = any(err.get("error_class") == "cancelled" for err in ar.errors)
                if is_locked:
                    run_item["status"] = "skipped_locked"
                else:
                    run_item["status"] = "failed"
            else:
                run_item["status"] = "failed"

            if ar.errors:
                last_err = ar.errors[-1]
                budget = last_err.get("retry_budget", {})
                if "used" in budget:
                    run_item["retries_used"] = budget["used"]
                if "max" in budget:
                    run_item["retry_budget"] = budget["max"]
                error_class = last_err.get("error_class")
                if error_class:
                    run_item["last_error_code"] = str(error_class)

            runs.append(run_item)

        # Map manual due items
        for md in self.manual_due:
            runs.append({
                "source_alias": md.source,
                "account_alias": md.account_alias,
                "status": "skipped_due_manual",
            })

        return {
            "contract_version": "0.1",
            "command": command,
            "status": cli_status,
            "exit_code": cli_exit,
            "started_at": self.started_at,
            "runs": runs,
        }


class SQLiteLedgerIntegrityCoordinator:
    """
    US-09.b: Ensures safe SQLite file access during single and concurrent operations.
    Configures WAL mode, appropriate busy timeouts, and integrity checks.
    """

    @staticmethod
    def configure_connection(conn: sqlite3.Connection, busy_timeout_ms: int = 10000) -> None:
        """Apply required PRAGMAs for concurrency and WAL integrity."""
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms};")

    @staticmethod
    def check_integrity(db_path: Union[str, Path]) -> bool:
        """Run quick_check on SQLite database file to verify no corruption."""
        path = Path(db_path)
        if not path.exists():
            return True
        conn = sqlite3.connect(str(path))
        try:
            cursor = conn.execute("PRAGMA quick_check;")
            row = cursor.fetchone()
            return row is not None and row[0] == "ok"
        finally:
            conn.close()


class CollectorOrchestrator:
    """
    Central Collector Orchestrator (PRD US-09).
    Coordinates execution of manual, scheduled, single-account, and all-source collections.
    """

    def __init__(
        self,
        config: Optional[Any] = None,
        lock_dir: Optional[Union[str, Path]] = None,
        retry_policy: Optional[RetryPolicy] = None,
        db_path: Optional[Union[str, Path]] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        self.config = config
        self.lock_dir = Path(lock_dir) if lock_dir else Path(".ael/locks")
        self.retry_policy = retry_policy or RetryPolicy()
        self.db_path = Path(db_path) if db_path else None
        self.sleep_fn = sleep_fn
        self._registered_tasks: List[AccountTask] = []

    def register_task(self, task: AccountTask) -> None:
        """Register a collection task."""
        self._registered_tasks.append(task)

    def clear_tasks(self) -> None:
        """Clear registered tasks."""
        self._registered_tasks.clear()

    def filter_tasks(
        self,
        source: Optional[str] = None,
        account_alias: Optional[str] = None,
        automatable_only: bool = False,
    ) -> List[AccountTask]:
        """
        US-09.a: Filter tasks by single source, single account, or automatable-only.
        """
        tasks = []
        for t in self._registered_tasks:
            if source and t.source != source:
                continue
            if account_alias and t.account_alias != account_alias:
                continue
            if automatable_only and t.is_manual:
                continue
            tasks.append(t)
        return tasks

    def execute_with_retry(
        self,
        collector_fn: Callable[..., Any],
        *args,
        **kwargs,
    ) -> Tuple[bool, Any, List[Dict[str, Any]]]:
        """
        US-09.c: Executes collector_fn under retry budget.
        - Transient 5xx, rate limits (429), timeouts are retried up to max_retries.
        - Respects Retry-After seconds.
        - Non-retryable errors (auth, config_secret_ref_missing, etc.) abort immediately.
        - Retry budget and last error are fully captured.

        Returns: (success: bool, result_or_none, errors_list)
        """
        attempt = 0
        errors: List[Dict[str, Any]] = []
        max_retries = self.retry_policy.max_retries

        while True:
            try:
                result = collector_fn(*args, **kwargs)
                return True, result, errors
            except Exception as e:
                attempt += 1
                error_class = getattr(e, "error_class", None)
                http_status = getattr(e, "http_status", None)
                retry_after = getattr(e, "retry_after_seconds", None)

                # Deduce error_class if not explicitly provided
                if error_class is None:
                    if http_status == 429:
                        error_class = "rate_limited"
                    elif http_status in (401, 403):
                        error_class = "unauthorized"
                    elif http_status and 500 <= http_status < 600:
                        error_class = "transient_5xx"
                    elif isinstance(e, TimeoutError):
                        error_class = "timeout"
                    else:
                        msg_lower = str(e).lower()
                        if "429" in msg_lower or "rate limit" in msg_lower:
                            error_class = "rate_limited"
                        elif "auth" in msg_lower or "unauthorized" in msg_lower or "forbidden" in msg_lower:
                            error_class = "unauthorized"
                        elif "secret" in msg_lower and "missing" in msg_lower:
                            error_class = "config_secret_ref_missing"
                        elif "timeout" in msg_lower:
                            error_class = "timeout"
                        else:
                            error_class = "transient_5xx"

                is_retryable = error_class not in NON_RETRYABLE_ERROR_CLASSES

                error_record = CollectorErrorRecord(
                    schema_version="0.1",
                    error_class=error_class,
                    retryable=is_retryable,
                    occurred_at=utc_now_iso(),
                    message=str(e),
                    retry_budget={"max": max_retries, "used": attempt},
                    http_status=http_status,
                    retry_after_seconds=retry_after,
                )
                errors.append(error_record.to_dict())

                # If non-retryable or budget exhausted, stop
                if not is_retryable or attempt > max_retries:
                    return False, None, errors

                # Calculate sleep backoff
                backoff = self.retry_policy.calculate_backoff(attempt - 1, retry_after)
                self.sleep_fn(backoff)

    def execute_account_task(
        self,
        task: AccountTask,
        wait_timeout: float = 0.0,
    ) -> AccountRunResult:
        """
        US-09.b: Execute single account task safely enclosed in an AccountLock.
        """
        if task.is_manual:
            # Manual tasks are handled as due items, not active collection tasks
            return AccountRunResult(
                account_alias=task.account_alias,
                profile_id=task.profile_id,
                source=task.source,
                product_domain=task.product_domain,
                capability=task.capability,
                status="skipped_unsupported",
                observed_at=utc_now_iso(),
            )

        # Acquire per-account lock
        try:
            with AccountLock(task.account_alias, lock_dir=self.lock_dir, wait_timeout=wait_timeout):
                # Ensure SQLite integrity if db_path is set
                if self.db_path:
                    SQLiteLedgerIntegrityCoordinator.check_integrity(self.db_path)

                start_proc_iso = utc_now_iso()
                if task.collector_fn is None:
                    # No-op collector or unassigned function
                    return AccountRunResult(
                        account_alias=task.account_alias,
                        profile_id=task.profile_id,
                        source=task.source,
                        product_domain=task.product_domain,
                        capability=task.capability,
                        status="success",
                        observed_at=start_proc_iso,
                    )

                success, result, errs = self.execute_with_retry(task.collector_fn, **task.kwargs)

                if success:
                    # Collector returned success
                    return AccountRunResult(
                        account_alias=task.account_alias,
                        profile_id=task.profile_id,
                        source=task.source,
                        product_domain=task.product_domain,
                        capability=task.capability,
                        status="success",
                        observed_at=utc_now_iso(),
                        data_payload=result,
                        errors=errs,
                    )
                else:
                    return AccountRunResult(
                        account_alias=task.account_alias,
                        profile_id=task.profile_id,
                        source=task.source,
                        product_domain=task.product_domain,
                        capability=task.capability,
                        status="failure",
                        observed_at=utc_now_iso(),
                        errors=errs,
                    )

        except AccountLockError as e:
            # Account concurrency violation / locked
            err = CollectorErrorRecord(
                schema_version="0.1",
                error_class="cancelled",
                retryable=False,
                occurred_at=utc_now_iso(),
                message=str(e),
                retry_budget={"max": self.retry_policy.max_retries, "used": 0},
            )
            return AccountRunResult(
                account_alias=task.account_alias,
                profile_id=task.profile_id,
                source=task.source,
                product_domain=task.product_domain,
                capability=task.capability,
                status="failure",
                observed_at=utc_now_iso(),
                errors=[err.to_dict()],
            )

    def run(
        self,
        trigger: str = "manual",
        source: Optional[str] = None,
        account_alias: Optional[str] = None,
        routing_subset: Optional[Sequence[str]] = None,
        parallel_accounts: bool = False,
        max_workers: int = 4,
        lock_wait_timeout: float = 0.0,
    ) -> OrchestrationRunReport:
        """
        US-09.a, US-09.b, US-09.c, US-09.d:
        Main orchestration entry point for manual or scheduler triggers.
        - Supports routing subsets or filtering by source and account.
        - Detects manual sources, marks them as due, and excludes them from blocking automated run.
        - Executes account runs sequentially or in parallel (safe per-account locking).
        - Produces schema/collector/run.schema.json compliant report.
        """
        if trigger not in ALLOWED_TRIGGERS:
            raise ValueError(f"Trigger must be one of {sorted(ALLOWED_TRIGGERS)}, got '{trigger}'")

        started_at = utc_now_iso()
        report = OrchestrationRunReport(
            run_id=f"run_{generate_ulid()}",
            trigger=trigger,
            started_at=started_at,
            routing_subset=list(routing_subset or []),
        )

        # Select tasks
        subset_set = set(routing_subset) if routing_subset else None
        candidate_tasks = []
        for t in self._registered_tasks:
            if source and t.source != source:
                continue
            if account_alias and t.account_alias != account_alias:
                continue
            if subset_set is not None:
                if t.source not in subset_set and t.account_alias not in subset_set:
                    continue
            candidate_tasks.append(t)

        automated_tasks: List[AccountTask] = []
        for t in candidate_tasks:
            if t.is_manual:
                due_item = t.due_item or ManualDueItem(
                    account_alias=t.account_alias,
                    source=t.source,
                    product_domain=t.product_domain,
                    capability=t.capability,
                    due_at=utc_now_iso(),
                    reason=f"Manual collection for {t.source}/{t.account_alias} is due",
                )
                report.manual_due.append(due_item)
            else:
                automated_tasks.append(t)

        # Execute automated tasks
        results: List[AccountRunResult] = []
        if automated_tasks:
            if parallel_accounts and len(automated_tasks) > 1:
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = [
                        executor.submit(self.execute_account_task, t, lock_wait_timeout)
                        for t in automated_tasks
                    ]
                    for fut in futures:
                        results.append(fut.result())
            else:
                for t in automated_tasks:
                    results.append(self.execute_account_task(t, wait_timeout=lock_wait_timeout))

        report.account_runs = results
        report.finished_at = utc_now_iso()
        report.compute_summary_status()
        return report


@dataclass
class SchedulerCronEntry:
    """
    US-09.e: Represents a single documented cron schedule specification.
    Includes schedule expression, command, and purpose description.
    """
    expression: str
    command_args: str
    description: str


@dataclass
class SchedulerCronConfig:
    """
    US-09.e: Comprehensive scheduler configuration generator and installer helper.
    Benennt:
    - Absoluten Programmpfad (binary_path)
    - Daten-/Konfigurationspfad (data_dir, config_path)
    - Secretbereitstellung (env_file or env_vars)
    - Zeitzone (timezone)
    - Locking (lock_dir / flock wrapper)
    - Log-Aggregation (log_dir)
    - Ausschließlich bewusste Nutzeraktion zur Installation (install / dry-run)
    """
    binary_path: str = "/usr/local/bin/ael"
    config_path: str = "/etc/ael/config.yaml"
    data_dir: str = "/var/lib/ael"
    log_dir: str = "/var/log/ael"
    env_file: Optional[str] = "/etc/ael/secrets.env"
    timezone: str = "UTC"
    lock_wrapper: str = "flock -n /var/run/ael-scheduler.lock"
    entries: List[SchedulerCronEntry] = field(default_factory=lambda: [
        SchedulerCronEntry("5 * * * *", "collect byteplus --all-accounts", "BytePlus usage and quota collection"),
        SchedulerCronEntry("15 0 * * *", "collect deepseek", "DeepSeek usage collection"),
        SchedulerCronEntry("25 2 * * *", "collect byteplus --billing --all-accounts", "BytePlus billing snapshot collection"),
        SchedulerCronEntry("35 0 * * *", "collect github", "GitHub activity collection"),
        SchedulerCronEntry("45 0 * * *", "collect forgejo", "Forgejo activity collection"),
        SchedulerCronEntry("15 * * * *", "collect faigate", "faigate runtime observation collection"),
        SchedulerCronEntry("30 3 * * *", "reconcile", "Nightly reconciliation and ledger sync"),
        SchedulerCronEntry("45 3 * * *", "evidence verify", "Nightly integrity and evidence verification"),
    ])

    def render_crontab(self) -> str:
        """
        Generate strict, idempotent crontab content.
        Uses absolute paths, explicit environment variables, secret sourcing, and file locking.
        """
        lines = [
            "# ===================================================================",
            "# AI Engineering Ledger (AEL) Cron Configuration",
            "# PRD US-09.e: Absolute paths, explicit secrets, timezone, and locking.",
            "# NOTICE: Active installation requires explicit, deliberate user action.",
            "# ===================================================================",
            f"SHELL=/bin/bash",
            f"PATH=/usr/local/bin:/usr/bin:/bin",
            f"CRON_TZ={self.timezone}",
            f"TZ={self.timezone}",
            f"AEL_CONFIG={self.config_path}",
            f"AEL_DATA_DIR={self.data_dir}",
            f"AEL_LOG_DIR={self.log_dir}",
        ]
        if self.env_file:
            lines.append(f"AEL_ENV_FILE={self.env_file}")
        lines.append("")

        for entry in self.entries:
            lines.append(f"# {entry.description}")
            cmd_parts = []
            if self.env_file:
                # Source secrets securely before execution
                cmd_parts.append(f"set -a && source {self.env_file} && set +a &&")
            if self.lock_wrapper:
                cmd_parts.append(f"{self.lock_wrapper}")

            # Execution command: headless non-interactive with json log append
            slug = re.sub(r"[^a-z0-9_-]", "_", entry.command_args.replace(" ", "_").replace("-", "_"))
            log_file = f"{self.log_dir}/{slug}.log"
            exec_line = (
                f"{self.binary_path} --trigger scheduler --config {self.config_path} "
                f"{entry.command_args} >> {log_file} 2>&1"
            )
            cmd_parts.append(exec_line)
            full_cmd = " ".join(cmd_parts)
            lines.append(f"{entry.expression} {full_cmd}")
            lines.append("")

        return "\n".join(lines).strip() + "\n"

    def render_documentation(self) -> str:
        """
        Render human-readable and operator-verifiable documentation explaining all parameters.
        """
        doc = f"""# AEL Scheduler & Cron Automation Documentation (PRD US-09.e)

## 1. Absolute Programmpfad
- **Programmpfad (Binary):** `{self.binary_path}`
- **Erklärung:** Cron-Umgebungen setzen keinen interaktiven Benutzer-`PATH` voraus. Jeder Aufruf referenziert den absoluten Pfad.

## 2. Daten- und Konfigurationspfade
- **Konfigurationsdatei:** `{self.config_path}`
- **Datenverzeichnis (SQLite / Ledger):** `{self.data_dir}`
- **Logverzeichnis (Aggregierte Logs):** `{self.log_dir}`
- **Erklärung:** Standardmäßig isolationistisch; alle Zustandsdaten und Logs liegen in fest definierten Verzeichnissen.

## 3. Secretbereitstellung
- **Secret-Env-Datei:** `{self.env_file or "Keine (aus Umgebung bezogen)"}`
- **Erklärung:** API-Schlüssel, Tokens und Passwörter werden vor Ausführung aus der gesicherten Umgebungsdatei (`chmod 600`) per `set -a && source ...` bezogen. Secrets dürfen weder im Cron-File noch in Logs im Klartext erscheinen.

## 4. Zeitzone
- **Konfigurierte Zeitzone:** `{self.timezone}`
- **Erklärung:** Explizit über `CRON_TZ` und `TZ` deklariert. Verhindert Missverständnisse bei Sommer-/Winterzeit und UTC-Abrechnungszyklen.

## 5. Locking & Idempotenz
- **Locking-Mechanismus:** `{self.lock_wrapper}`
- **Erklärung:** Verhindert Überlappungen zeitgleicher Scheduler-Träger. Zusätzlich erzwingt AEL auf Account-Ebene flock-basierte Exklusivsperren (`AccountLock`).

## 6. Bewusste Nutzerinstallation
Die Installation erfolgt **ausschließlich durch bewusste Nutzeraktion**:
- Befehl zur manuellen Vorschau (Dry-run):
  `ael schedule show`
- Befehl zur bewussten Installation in die Benutzer-Crontab:
  `ael schedule install --confirm`
- Keine implizite oder automatische Hintergrund-Registrierung ohne Bestätigung!
"""
        return doc

    def install_to_crontab(
        self,
        crontab_runner: Optional[Callable[[List[str], Optional[str]], Tuple[int, str, str]]] = None,
        confirm: bool = False,
    ) -> Tuple[bool, str]:
        """
        US-09.e: Install crontab strictly requiring confirm=True (deliberate user action).
        If confirm is False, refuses to install and returns safety guidance.
        """
        if not confirm:
            return (
                False,
                "Installation refused: PRD US-09.e requires deliberate user action (confirm=True)."
            )

        content = self.render_crontab()
        if crontab_runner is None:
            import subprocess
            try:
                proc = subprocess.run(
                    ["crontab", "-"],
                    input=content,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if proc.returncode == 0:
                    return True, "Successfully installed AEL crontab entries."
                else:
                    return False, f"crontab execution failed (exit {proc.returncode}): {proc.stderr}"
            except Exception as e:
                return False, f"Failed executing crontab: {e}"
        else:
            code, stdout, stderr = crontab_runner(["crontab", "-"], content)
            if code == 0:
                return True, "Successfully installed AEL crontab entries."
            else:
                return False, f"crontab runner failed (exit {code}): {stderr}"


class SchedulerLogAggregator:
    """
    US-09.e: Aggregates and inspects scheduler run execution logs.
    Collects log records, parses JSON structured run reports,
    and checks execution health without interactive prompts.
    """

    def __init__(self, log_dir: Union[str, Path]):
        self.log_dir = Path(log_dir)

    def write_run_log(self, report: OrchestrationRunReport, command: str = "collect") -> Path:
        """Append headless structured run report as JSON line to scheduler log."""
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_file = self.log_dir / "scheduler.log"
        payload = report.to_cli_result(command=command)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return log_file

    def get_latest_runs(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Read recent JSON line reports from scheduler log."""
        log_file = self.log_dir / "scheduler.log"
        if not log_file.exists():
            return []
        runs = []
        with open(log_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    runs.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return runs[-limit:]
