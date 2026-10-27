"""
Tests for collectors/orchestration (AEL-US09-01).
Covers US-09.a, US-09.b, US-09.c, and schema validation against run.schema.json.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from unittest import TestCase
import unittest

from collectors.orchestration import (
    AccountLock,
    AccountLockError,
    AccountRunResult,
    AccountTask,
    CollectorErrorRecord,
    CollectorOrchestrator,
    ManualDueItem,
    OrchestrationRunReport,
    RetryPolicy,
    SQLiteLedgerIntegrityCoordinator,
    STATUS_FAILURE,
    STATUS_PARTIAL_FAILURE,
    STATUS_SUCCESS,
    EXIT_FAILURE,
    EXIT_PARTIAL_FAILURE,
    EXIT_SUCCESS,
    EXIT_USAGE_ERROR,
    SchedulerCronConfig,
    SchedulerCronEntry,
    SchedulerLogAggregator,
    sanitize_error_message,
)


class TestOrchestrationUnifiedTrigger(TestCase):
    """
    US-09.a:
    - Einzelquelle, Einzelaccount und alle aktivierten automatisierbaren Quellen sind getrennt aufrufbar.
    - Manuelle Quellen werden als fällig ausgegeben und blockieren den automatischen Lauf nicht.
    - Trigger manual vs scheduler identisch aufrufbar.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.lock_dir = Path(self.temp_dir.name) / "locks"
        self.orchestrator = CollectorOrchestrator(lock_dir=self.lock_dir)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_single_source_and_single_account_filtering(self):
        """Verify single source, single account, and combinations are filterable."""
        t1 = AccountTask("bp01", "bp01", "byteplus", "byteplus_coding_plan", "quota", is_manual=False)
        t2 = AccountTask("bp02", "bp02", "byteplus", "byteplus_coding_plan", "quota", is_manual=False)
        t3 = AccountTask("gh_main", "gh_main", "github", "git", "git", is_manual=False)
        t4 = AccountTask("anthropic_user", "anthropic_user", "anthropic", "subscription", "manual", is_manual=True)

        for t in [t1, t2, t3, t4]:
            self.orchestrator.register_task(t)

        # Filter by single source 'github'
        gh_tasks = self.orchestrator.filter_tasks(source="github")
        self.assertEqual(len(gh_tasks), 1)
        self.assertEqual(gh_tasks[0].account_alias, "gh_main")

        # Filter by single account 'bp02'
        bp02_tasks = self.orchestrator.filter_tasks(account_alias="bp02")
        self.assertEqual(len(bp02_tasks), 1)
        self.assertEqual(bp02_tasks[0].source, "byteplus")

        # Filter automatable only (excludes manual)
        auto_tasks = self.orchestrator.filter_tasks(automatable_only=True)
        self.assertEqual(len(auto_tasks), 3)
        self.assertTrue(all(not t.is_manual for t in auto_tasks))

    def test_manual_sources_reported_as_due_and_do_not_block_automated_run(self):
        """
        US-09.a: Manual sources must be emitted as due (fällig),
        must not cause an error or block the automated execution,
        and overall run status must remain success if automated sources succeed.
        """
        t_auto = AccountTask(
            account_alias="bp01",
            profile_id="bp01",
            source="byteplus",
            product_domain="byteplus_coding_plan",
            capability="quota",
            is_manual=False,
            collector_fn=lambda: {"quota": 100},
        )
        t_manual = AccountTask(
            account_alias="anthropic_user",
            profile_id="anthropic_user",
            source="anthropic",
            product_domain="subscription",
            capability="manual",
            is_manual=True,
            due_item=ManualDueItem(
                account_alias="anthropic_user",
                source="anthropic",
                product_domain="subscription",
                capability="manual",
                reason="Monthly Anthropic invoice download is due",
            ),
        )

        self.orchestrator.register_task(t_auto)
        self.orchestrator.register_task(t_manual)

        report = self.orchestrator.run(trigger="scheduler")

        # Verify automated run executed and succeeded
        self.assertEqual(len(report.account_runs), 1)
        self.assertEqual(report.account_runs[0].account_alias, "bp01")
        self.assertEqual(report.account_runs[0].status, "success")

        # Verify manual source was emitted as due
        self.assertEqual(len(report.manual_due), 1)
        self.assertEqual(report.manual_due[0].account_alias, "anthropic_user")
        self.assertEqual(report.manual_due[0].reason, "Monthly Anthropic invoice download is due")

        # Verify overall status is success and exit code is 0 (ADR-0002)
        self.assertEqual(report.status, STATUS_SUCCESS)
        self.assertEqual(report.exit_status, EXIT_SUCCESS)

    def test_unified_manual_and_scheduler_trigger(self):
        """Verify identical execution path whether triggered manually or via scheduler."""
        task = AccountTask("bp01", "bp01", "byteplus", "byteplus_billing", "billing", is_manual=False)
        self.orchestrator.register_task(task)

        manual_report = self.orchestrator.run(trigger="manual")
        scheduler_report = self.orchestrator.run(trigger="scheduler")

        self.assertEqual(manual_report.trigger, "manual")
        self.assertEqual(scheduler_report.trigger, "scheduler")
        self.assertEqual(manual_report.status, STATUS_SUCCESS)
        self.assertEqual(scheduler_report.status, STATUS_SUCCESS)

    def test_negative_invalid_trigger_raises_error(self):
        """Negative test: unsupported trigger string raises ValueError."""
        with self.assertRaises(ValueError):
            self.orchestrator.run(trigger="invalid_trigger")


class TestOrchestrationConcurrencyAndLocking(TestCase):
    """
    US-09.b:
    - Gleichzeitige Läufe desselben Accounts werden gesperrt oder sicher serialisiert.
    - Keine doppelte aktive Messung.
    - Keine beschädigte SQLite-Datei (WAL, PRAGMA checks).
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.lock_dir = Path(self.temp_dir.name) / "locks"
        self.db_path = Path(self.temp_dir.name) / "ledger.db"

        # Initialize SQLite database with table
        conn = sqlite3.connect(str(self.db_path))
        SQLiteLedgerIntegrityCoordinator.configure_connection(conn)
        conn.execute("CREATE TABLE test_measurements (id INTEGER PRIMARY KEY, account TEXT, val REAL);")
        conn.commit()
        conn.close()

        self.orchestrator = CollectorOrchestrator(
            lock_dir=self.lock_dir,
            db_path=self.db_path,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_concurrent_run_same_account_is_blocked_without_wait(self):
        """Negative test: concurrent execution on the same account raises AccountLockError immediately."""
        lock1 = AccountLock("bp01", lock_dir=self.lock_dir, wait_timeout=0.0)
        lock2 = AccountLock("bp01", lock_dir=self.lock_dir, wait_timeout=0.0)

        # Acquire lock1
        self.assertTrue(lock1.acquire())

        # Attempt to acquire lock2 on the same account -> must fail with AccountLockError
        with self.assertRaises(AccountLockError) as ctx:
            lock2.acquire()
        self.assertIn("Concurrent run active on account 'bp01'", str(ctx.exception))

        lock1.release()

        # After release, lock2 can now acquire
        self.assertTrue(lock2.acquire())
        lock2.release()

    def test_concurrent_run_same_account_serialized_with_wait(self):
        """Positive test: concurrent execution on same account safely serializes when timeout is given."""
        execution_log = []

        def slow_worker(idx: int, delay: float):
            with AccountLock("bp01", lock_dir=self.lock_dir, wait_timeout=2.0):
                execution_log.append(f"start_{idx}")
                time.sleep(delay)
                execution_log.append(f"end_{idx}")

        t1 = threading.Thread(target=slow_worker, args=(1, 0.2))
        t2 = threading.Thread(target=slow_worker, args=(2, 0.1))

        t1.start()
        time.sleep(0.05)  # Ensure t1 starts first and acquires lock
        t2.start()

        t1.join()
        t2.join()

        # Assert clean serialization: start_1 -> end_1 -> start_2 -> end_2
        self.assertEqual(execution_log, ["start_1", "end_1", "start_2", "end_2"])

    def test_different_accounts_can_run_concurrently(self):
        """Different accounts must not block each other."""
        lock_bp01 = AccountLock("bp01", lock_dir=self.lock_dir, wait_timeout=0.0)
        lock_bp02 = AccountLock("bp02", lock_dir=self.lock_dir, wait_timeout=0.0)

        self.assertTrue(lock_bp01.acquire())
        self.assertTrue(lock_bp02.acquire())

        lock_bp01.release()
        lock_bp02.release()

    def test_sqlite_integrity_under_parallel_writes(self):
        """Verify SQLite database integrity is preserved under multi-threaded writes with WAL coordinator."""
        def write_db(account: str, count: int):
            for i in range(count):
                with AccountLock(account, lock_dir=self.lock_dir, wait_timeout=5.0):
                    conn = sqlite3.connect(str(self.db_path))
                    SQLiteLedgerIntegrityCoordinator.configure_connection(conn)
                    conn.execute("INSERT INTO test_measurements (account, val) VALUES (?, ?);", (account, float(i)))
                    conn.commit()
                    conn.close()

        threads = []
        for acc in ["bp01", "bp02", "bp03", "bp04"]:
            t = threading.Thread(target=write_db, args=(acc, 20))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # Verify integrity PRAGMA
        self.assertTrue(SQLiteLedgerIntegrityCoordinator.check_integrity(self.db_path))

        conn = sqlite3.connect(str(self.db_path))
        count = conn.execute("SELECT count(*) FROM test_measurements;").fetchone()[0]
        conn.close()
        self.assertEqual(count, 80)


class CustomAuthException(Exception):
    def __init__(self, msg="Invalid API credentials Bearer secret_12345678"):
        super().__init__(msg)
        self.error_class = "unauthorized"
        self.http_status = 401


class CustomRateLimitException(Exception):
    def __init__(self, msg="Rate limited 429", retry_after=1):
        super().__init__(msg)
        self.error_class = "rate_limited"
        self.http_status = 429
        self.retry_after_seconds = retry_after


class Custom5xxException(Exception):
    def __init__(self, msg="Internal Server Error"):
        super().__init__(msg)
        self.error_class = "transient_5xx"
        self.http_status = 502


class TestOrchestrationRetryAndBudget(TestCase):
    """
    US-09.c:
    - 429/temporäre 5xx führen zu begrenzten Wiederholungen unter Beachtung von `Retry-After`.
    - Authfehler erzeugen keine Endlosschleife (sofortiger Abbruch).
    - Retrybudget und letzter Fehler sind sichtbar.
    - Fehler enthalten keine Secret-Leaks.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.lock_dir = Path(self.temp_dir.name) / "locks"
        self.sleep_calls = []

        def mock_sleep(seconds: float):
            self.sleep_calls.append(seconds)

        self.mock_sleep = mock_sleep
        self.retry_policy = RetryPolicy(
            max_retries=3,
            initial_backoff_seconds=0.1,
            backoff_multiplier=2.0,
            respect_retry_after=True,
        )
        self.orchestrator = CollectorOrchestrator(
            lock_dir=self.lock_dir,
            retry_policy=self.retry_policy,
            sleep_fn=self.mock_sleep,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_auth_error_aborts_immediately_without_retry(self):
        """Negative test: Auth / 401 / 403 must not retry; abort immediately."""
        call_count = [0]

        def failing_auth_collector():
            call_count[0] += 1
            raise CustomAuthException()

        success, result, errors = self.orchestrator.execute_with_retry(failing_auth_collector)

        self.assertFalse(success)
        self.assertIsNone(result)
        self.assertEqual(call_count[0], 1)  # Called only once! No retry.
        self.assertEqual(len(self.sleep_calls), 0)  # No backoff sleeps.

        self.assertEqual(len(errors), 1)
        err = errors[0]
        self.assertEqual(err["error_class"], "unauthorized")
        self.assertFalse(err["retryable"])
        self.assertEqual(err["retry_budget"]["max"], 3)
        self.assertEqual(err["retry_budget"]["used"], 1)
        # Verify secret redaction
        self.assertNotIn("Bearer secret_12345678", err["message"])
        self.assertIn("[REDACTED]", err["message"])

    def test_rate_limited_429_respects_retry_after(self):
        """Positive test: 429 retries with Retry-After header, eventually succeeds."""
        attempts = [0]

        def rate_limited_collector():
            attempts[0] += 1
            if attempts[0] < 3:
                raise CustomRateLimitException(retry_after=2)
            return {"status": "ok"}

        success, result, errors = self.orchestrator.execute_with_retry(rate_limited_collector)

        self.assertTrue(success)
        self.assertEqual(result, {"status": "ok"})
        self.assertEqual(attempts[0], 3)
        # Slept twice for 2 seconds (Retry-After)
        self.assertEqual(self.sleep_calls, [2.0, 2.0])
        # Two retry errors recorded
        self.assertEqual(len(errors), 2)
        self.assertEqual(errors[0]["error_class"], "rate_limited")
        self.assertEqual(errors[0]["retry_after_seconds"], 2)
        self.assertEqual(errors[0]["retry_budget"]["used"], 1)
        self.assertEqual(errors[1]["retry_budget"]["used"], 2)

    def test_transient_5xx_retries_exponential_backoff_up_to_budget(self):
        """Negative test: 5xx fails continuously until retry budget is exhausted."""
        attempts = [0]

        def persistent_5xx_collector():
            attempts[0] += 1
            raise Custom5xxException("502 Bad Gateway")

        success, result, errors = self.orchestrator.execute_with_retry(persistent_5xx_collector)

        self.assertFalse(success)
        self.assertIsNone(result)
        # Attempt 1, then 3 retries = 4 total attempts
        self.assertEqual(attempts[0], 4)
        # 3 sleep calls with exponential backoff: 0.1, 0.2, 0.4
        self.assertEqual(len(self.sleep_calls), 3)
        self.assertAlmostEqual(self.sleep_calls[0], 0.1)
        self.assertAlmostEqual(self.sleep_calls[1], 0.2)
        self.assertAlmostEqual(self.sleep_calls[2], 0.4)

        # 4 error records
        self.assertEqual(len(errors), 4)
        last_error = errors[-1]
        self.assertEqual(last_error["error_class"], "transient_5xx")
        self.assertTrue(last_error["retryable"])
        self.assertEqual(last_error["retry_budget"]["max"], 3)
        self.assertEqual(last_error["retry_budget"]["used"], 4)

    def test_secret_sanitization_in_error_records(self):
        """Verify sanitize_error_message removes various token patterns."""
        raw_msg = "Failed with Bearer secret_token_abc and sk-998877665544 and secret=pass123 password=mysecret"
        sanitized = sanitize_error_message(raw_msg)
        self.assertNotIn("secret_token_abc", sanitized)
        self.assertNotIn("998877665544", sanitized)
        self.assertNotIn("pass123", sanitized)
        self.assertNotIn("mysecret", sanitized)


class TestOrchestrationReportingAndSchema(TestCase):
    """
    US-09.d:
    - Aggregated run reporting according to schema/collector/run.schema.json & ADR-0002.
    - Exit statuses: 0=success, 3=partial_failure, 1=failure.
    - Fully validated against JSON Schema draft 2020-12.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.lock_dir = Path(self.temp_dir.name) / "locks"
        self.orchestrator = CollectorOrchestrator(lock_dir=self.lock_dir)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_partial_failure_exit_status_3(self):
        """
        US-09.d & ADR-0002: If one account succeeds and one fails,
        status must be partial_failure and exit_status must be 3.
        """
        t1 = AccountTask(
            account_alias="bp01",
            profile_id="bp01",
            source="byteplus",
            product_domain="byteplus_coding_plan",
            capability="quota",
            is_manual=False,
            collector_fn=lambda: {"quota": 50},
        )
        t2 = AccountTask(
            account_alias="bp02",
            profile_id="bp02",
            source="byteplus",
            product_domain="byteplus_coding_plan",
            capability="quota",
            is_manual=False,
            collector_fn=lambda: (_ for _ in ()).throw(Custom5xxException("500 Server Error")),
        )

        self.orchestrator.register_task(t1)
        self.orchestrator.register_task(t2)

        report = self.orchestrator.run(trigger="scheduler")

        self.assertEqual(report.status, STATUS_PARTIAL_FAILURE)
        self.assertEqual(report.exit_status, EXIT_PARTIAL_FAILURE)
        self.assertEqual(len(report.account_runs), 2)
        report_dict = report.to_dict()
        self.assertEqual(report_dict["status"], "partial_failure")
        self.assertEqual(report_dict["exit_status"], 3)

    def test_total_failure_exit_status_1(self):
        """
        US-09.d & ADR-0002: If all account runs fail,
        status must be failure and exit_status must be 1.
        """
        t1 = AccountTask(
            account_alias="bp01",
            profile_id="bp01",
            source="byteplus",
            product_domain="byteplus_coding_plan",
            capability="quota",
            is_manual=False,
            collector_fn=lambda: (_ for _ in ()).throw(CustomAuthException("401 Unauthorized")),
        )
        self.orchestrator.register_task(t1)

        report = self.orchestrator.run(trigger="manual")

        self.assertEqual(report.status, STATUS_FAILURE)
        self.assertEqual(report.exit_status, EXIT_FAILURE)
        report_dict = report.to_dict()
        self.assertEqual(report_dict["status"], "failure")
        self.assertEqual(report_dict["exit_status"], 1)

    def test_run_report_conforms_to_json_schema(self):
        """
        Verify that generated run report dict passes jsonschema validation
        against schema/collector/run.schema.json.
        """
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema library not installed in test environment")

        # Load schemas from worktree or main repo root
        candidates = [
            Path(__file__).resolve().parents[3] / "schema" / "collector" / "run.schema.json",
            Path(__file__).resolve().parents[6] / "schema" / "collector" / "run.schema.json",
        ]
        schema_path = None
        for c in candidates:
            if c.exists():
                schema_path = c
                break

        if schema_path is None:
            self.skipTest(f"run.schema.json not found in candidate paths: {candidates}")

        with open(schema_path, "r", encoding="utf-8") as f:
            run_schema = json.load(f)

        collector_dir = schema_path.parent
        common_schema_path = collector_dir / "common.schema.json"
        with open(common_schema_path, "r", encoding="utf-8") as f:
            common_schema = json.load(f)

        error_schema_path = collector_dir / "error.schema.json"
        with open(error_schema_path, "r", encoding="utf-8") as f:
            error_schema = json.load(f)

        # Build schema store
        schema_store = {
            "run.schema.json": run_schema,
            "https://ael.local/schema/collector/run.schema.json": run_schema,
            "common.schema.json": common_schema,
            "https://ael.local/schema/collector/common.schema.json": common_schema,
            "error.schema.json": error_schema,
            "https://ael.local/schema/collector/error.schema.json": error_schema,
        }

        # Create sample successful report with process capture
        report = OrchestrationRunReport(
            trigger="scheduler",
            routing_subset=["byteplus"],
        )
        ar = AccountRunResult(
            account_alias="bp01",
            profile_id="bp01",
            source="byteplus",
            product_domain="byteplus_billing",
            capability="billing",
            status="success",
            observed_at=report.started_at,
            coverage={
                "requested": {"start": "2026-10-01T00:00:00Z", "end": "2026-10-26T00:00:00Z"},
                "delivered": {"start": "2026-10-01T00:00:00Z", "end": "2026-10-26T00:00:00Z"},
                "confirmed": {"start": "2026-10-01T00:00:00Z", "end": "2026-10-26T00:00:00Z"},
                "confirmation": "complete",
                "pagination": {
                    "pages_fetched": 1,
                    "complete": True,
                    "truncated": False,
                    "next_cursor": None,
                },
            },
            process={
                "argv": ["arkcli", "--profile", "bp01", "billing", "list"],
                "exit_code": 0,
                "signal": None,
                "stdout_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                "stderr_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                "stdout_truncated": False,
                "stderr_truncated": False,
                "started_at": report.started_at,
                "finished_at": report.finished_at,
                "profile_id": "bp01",
            },
        )
        report.account_runs.append(ar)

        report_dict = report.to_dict()

        # Validate with jsonschema
        resolver = jsonschema.RefResolver.from_schema(run_schema, store=schema_store)
        validator = jsonschema.Draft202012Validator(run_schema, resolver=resolver)
        errors = list(validator.iter_errors(report_dict))
        self.assertEqual(errors, [], f"Schema validation errors: {errors}")


class TestOrchestrationCliResultAndExportContract(TestCase):
    """
    US-09.d:
    - CLI result JSON schema conformance (schema/export/cli-result.schema.json).
    - Status and exit code coupling: success->0, partial->3, failure->1.
    - Handling of manual due sources as skipped_due_manual (exit 0).
    - Handling of lock conflicts as skipped_locked.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.lock_dir = Path(self.temp_dir.name) / "locks"
        self.orchestrator = CollectorOrchestrator(lock_dir=self.lock_dir)

        # Locate cli-result.schema.json
        candidates = [
            Path(__file__).resolve().parents[3] / "schema" / "export" / "cli-result.schema.json",
            Path(__file__).resolve().parents[6] / "schema" / "export" / "cli-result.schema.json",
        ]
        self.cli_schema_path = None
        for c in candidates:
            if c.exists():
                self.cli_schema_path = c
                break

    def tearDown(self):
        self.temp_dir.cleanup()

    def _validate_cli_schema(self, payload: dict):
        if self.cli_schema_path is None:
            return
        try:
            import jsonschema
            with open(self.cli_schema_path, "r", encoding="utf-8") as f:
                schema = json.load(f)
            validator = jsonschema.Draft202012Validator(schema)
            errors = list(validator.iter_errors(payload))
            self.assertEqual(errors, [], f"CLI Schema validation errors: {errors}")
        except ImportError:
            pass

    def test_cli_result_success(self):
        """US-09.d: Full success results in status 'success', exit_code 0."""
        t1 = AccountTask(
            account_alias="deepseek-main",
            profile_id="deepseek-main",
            source="deepseek",
            product_domain="deepseek_usage",
            capability="usage",
            is_manual=False,
            collector_fn=lambda: {"usage": 42},
        )
        self.orchestrator.register_task(t1)
        report = self.orchestrator.run(trigger="manual")
        cli_result = report.to_cli_result(command="collect deepseek")

        self.assertEqual(cli_result["status"], "success")
        self.assertEqual(cli_result["exit_code"], 0)
        self.assertEqual(len(cli_result["runs"]), 1)
        self.assertEqual(cli_result["runs"][0]["status"], "success")
        self._validate_cli_schema(cli_result)

    def test_cli_result_partial_with_manual_and_failed(self):
        """US-09.d: Mixed result (1 success, 1 failure, 1 manual due) -> status 'partial', exit_code 3."""
        t1 = AccountTask(
            account_alias="bp01",
            profile_id="bp01",
            source="byteplus",
            product_domain="byteplus_coding_plan",
            capability="quota",
            is_manual=False,
            collector_fn=lambda: (_ for _ in ()).throw(Custom5xxException("500 Server Error")),
        )
        t2 = AccountTask(
            account_alias="forge-main",
            profile_id="forge-main",
            source="forgejo",
            product_domain="git",
            capability="git",
            is_manual=False,
            collector_fn=lambda: {"commits": 10},
        )
        t3 = AccountTask(
            account_alias="deepseek-main",
            profile_id="deepseek-main",
            source="deepseek",
            product_domain="subscription",
            capability="manual",
            is_manual=True,
            due_item=ManualDueItem(
                account_alias="deepseek-main",
                source="deepseek",
                product_domain="subscription",
                capability="manual",
            ),
        )
        for t in [t1, t2, t3]:
            self.orchestrator.register_task(t)

        report = self.orchestrator.run(trigger="scheduler")
        cli_result = report.to_cli_result(command="collect --all")

        self.assertEqual(cli_result["status"], "partial")
        self.assertEqual(cli_result["exit_code"], 3)
        self.assertEqual(len(cli_result["runs"]), 3)

        statuses = {r["account_alias"]: r["status"] for r in cli_result["runs"]}
        self.assertEqual(statuses["bp01"], "failed")
        self.assertEqual(statuses["forge-main"], "success")
        self.assertEqual(statuses["deepseek-main"], "skipped_due_manual")
        self._validate_cli_schema(cli_result)

    def test_cli_result_failure_and_lock_skipped(self):
        """US-09.d: All runs failing, including lock conflict -> status 'failure', exit_code 1."""
        # Hold lock externally to simulate concurrency collision
        lock = AccountLock("bp01", lock_dir=self.lock_dir, wait_timeout=0.0)
        self.assertTrue(lock.acquire())

        t1 = AccountTask(
            account_alias="bp01",
            profile_id="bp01",
            source="byteplus",
            product_domain="byteplus_coding_plan",
            capability="quota",
            is_manual=False,
            collector_fn=lambda: {"quota": 10},
        )
        self.orchestrator.register_task(t1)
        report = self.orchestrator.run(trigger="scheduler", lock_wait_timeout=0.0)
        lock.release()

        cli_result = report.to_cli_result(command="collect byteplus")
        self.assertEqual(cli_result["status"], "failure")
        self.assertEqual(cli_result["exit_code"], 1)
        self.assertEqual(cli_result["runs"][0]["status"], "skipped_locked")
        self._validate_cli_schema(cli_result)


class TestSchedulerCronAutomationAndLogAggregation(TestCase):
    """
    US-09.e:
    - Cron documentation specifies absolute program path, data/config paths,
      explicit secrets sourcing, timezone, and file locking.
    - Installation strictly requires deliberate user action (confirm=True).
    - Headless execution and log aggregation.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.temp_dir.name) / "logs"
        self.cron_config = SchedulerCronConfig(
            binary_path="/opt/ael/bin/ael",
            config_path="/etc/ael/config.yaml",
            data_dir="/var/lib/ael",
            log_dir=str(self.log_dir),
            env_file="/etc/ael/secrets.env",
            timezone="UTC",
            lock_wrapper="flock -n /var/run/ael-scheduler.lock",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_cron_configuration_content_adheres_to_us09_e(self):
        """
        US-09.e:
        - Benennt absoluten Programmpfad
        - Daten-/Konfigurationspfad
        - Secretbereitstellung
        - Zeitzone
        - Locking
        """
        crontab_str = self.cron_config.render_crontab()

        # 1. Absolute program path
        self.assertIn("/opt/ael/bin/ael", crontab_str)

        # 2. Data and config paths
        self.assertIn("AEL_CONFIG=/etc/ael/config.yaml", crontab_str)
        self.assertIn("AEL_DATA_DIR=/var/lib/ael", crontab_str)
        self.assertIn(f"AEL_LOG_DIR={self.log_dir}", crontab_str)

        # 3. Secret provision
        self.assertIn("AEL_ENV_FILE=/etc/ael/secrets.env", crontab_str)
        self.assertIn("set -a && source /etc/ael/secrets.env && set +a", crontab_str)

        # 4. Timezone
        self.assertIn("CRON_TZ=UTC", crontab_str)
        self.assertIn("TZ=UTC", crontab_str)

        # 5. Locking
        self.assertIn("flock -n /var/run/ael-scheduler.lock", crontab_str)

    def test_documentation_renders_all_mandatory_elements(self):
        """US-09.e: Operator documentation explains all required settings."""
        doc = self.cron_config.render_documentation()
        self.assertIn("Absolute Programmpfad", doc)
        self.assertIn("/opt/ael/bin/ael", doc)
        self.assertIn("/etc/ael/config.yaml", doc)
        self.assertIn("/var/lib/ael", doc)
        self.assertIn("/etc/ael/secrets.env", doc)
        self.assertIn("UTC", doc)
        self.assertIn("flock -n /var/run/ael-scheduler.lock", doc)
        self.assertIn("bewusste Nutzeraktion", doc)

    def test_negative_crontab_installation_without_confirmation_refused(self):
        """
        Negative probe: Installation WITHOUT confirm=True MUST be refused.
        US-09.e: Installation erfolgt ausschließlich durch bewusste Nutzeraktion.
        """
        installed, message = self.cron_config.install_to_crontab(confirm=False)
        self.assertFalse(installed)
        self.assertIn("Installation refused", message)
        self.assertIn("deliberate user action", message)

    def test_positive_crontab_installation_with_confirmation(self):
        """Positive probe: With confirm=True and mock runner, installation succeeds."""
        mock_calls = []

        def mock_crontab_runner(cmd: list, stdin_content: str):
            mock_calls.append((cmd, stdin_content))
            return 0, "", ""

        installed, message = self.cron_config.install_to_crontab(
            crontab_runner=mock_crontab_runner,
            confirm=True,
        )
        self.assertTrue(installed)
        self.assertEqual(len(mock_calls), 1)
        self.assertEqual(mock_calls[0][0], ["crontab", "-"])
        self.assertIn("/opt/ael/bin/ael", mock_calls[0][1])

    def test_scheduler_log_aggregation_headless(self):
        """US-09.e: Headless execution appends structured JSON lines and aggregates history."""
        aggregator = SchedulerLogAggregator(log_dir=self.log_dir)

        # Run 1: success
        r1 = OrchestrationRunReport(trigger="scheduler")
        r1.account_runs.append(
            AccountRunResult(
                account_alias="bp01",
                profile_id="bp01",
                source="byteplus",
                product_domain="quota",
                capability="quota",
                status="success",
            )
        )
        aggregator.write_run_log(r1, command="collect byteplus")

        # Run 2: partial failure
        r2 = OrchestrationRunReport(trigger="scheduler")
        r2.account_runs.append(
            AccountRunResult(
                account_alias="bp01",
                profile_id="bp01",
                source="byteplus",
                product_domain="quota",
                capability="quota",
                status="success",
            )
        )
        r2.account_runs.append(
            AccountRunResult(
                account_alias="bp02",
                profile_id="bp02",
                source="byteplus",
                product_domain="quota",
                capability="quota",
                status="failure",
                errors=[{"error_class": "timeout", "retry_budget": {"max": 3, "used": 3}}],
            )
        )
        aggregator.write_run_log(r2, command="collect byteplus --all-accounts")

        # Read back latest runs
        latest = aggregator.get_latest_runs(limit=10)
        self.assertEqual(len(latest), 2)
        self.assertEqual(latest[0]["status"], "success")
        self.assertEqual(latest[0]["exit_code"], 0)
        self.assertEqual(latest[1]["status"], "partial")
        self.assertEqual(latest[1]["exit_code"], 3)
        self.assertEqual(latest[1]["runs"][1]["last_error_code"], "timeout")


if __name__ == "__main__":
    unittest.main()
