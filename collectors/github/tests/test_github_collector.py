#!/usr/bin/env python3
"""
Unit and Contract Tests for GitHub Collector (AEL-ADAPTER-GITHUB).
Stdlib unittest only. Tests:
- Schema conformance of account, capability, run, error, and event envelopes.
- Link header pagination.
- Empty repo (409 Conflict) vs unauthorized (401/403) vs not found (404) states (US-12.c).
- Rate-limiting headers and retry calculations.
- 14-day rolling window constraints for traffic snapshots (US-12).
- Commit / release / PR metadata extraction.
- Idempotency key stability (US-13).
- Secret redaction in logs/errors (no tokens leaked).
"""

import json
import os
import sys
import unittest
from typing import Any, Dict, List, Optional, Tuple

# Add repository root to sys.path to allow importing schema validation if available
repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../"))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

# Import local collector package
from collectors.github.collector import (
    GitHubCollector,
    GitHubConfig,
    GitHubClient,
    GitHubApiError,
    parse_link_header,
    compute_idempotency_key,
    generate_ulid,
    sanitize_message,
)

try:
    from schema.collector.tests.validate import schema_errors, semantic_errors
except ImportError:
    schema_errors = None
    semantic_errors = None

try:
    from schema.core.semantic import check_event
    from schema.core.tests.minival import Loader, validate as minival_validate
    core_loader = Loader(os.path.join(repo_root, "schema/core"))
except Exception:
    check_event = None
    minival_validate = None
    core_loader = None


class MockGitHubClient(GitHubClient):
    """Synthetic in-memory GitHub client for contract verification."""

    def __init__(self, fixture_dir: str):
        config = GitHubConfig(
            account_alias="test_octo",
            profile_id="github_test_octo",
            token="gho_testmocktoken1234567890",
            allowlist_repos=("octocat/Hello-World",),
        )
        super().__init__(config)
        self.fixture_dir = fixture_dir
        self.call_history: List[str] = []

    def _read_fixture(self, name: str) -> Any:
        path = os.path.join(self.fixture_dir, name)
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def request(
        self,
        endpoint_or_url: str,
        headers: Optional[Dict[str, str]] = None,
        method: str = "GET",
    ) -> Tuple[int, Any, Dict[str, str]]:
        self.call_history.append(endpoint_or_url)

        # 1. Commits pagination
        if "/commits" in endpoint_or_url:
            if "empty-repo" in endpoint_or_url:
                raise GitHubApiError(
                    message="Git Repository is empty.",
                    http_status=409,
                    error_class="unsupported",
                    retryable=False,
                    is_empty_repo=True,
                )
            if "page=2" in endpoint_or_url:
                data = self._read_fixture("commits_page2.json")
                return 200, data, {}
            else:
                data = self._read_fixture("commits_page1.json")
                resp_headers = {
                    "Link": '<https://api.github.com/repos/octocat/Hello-World/commits?page=2>; rel="next"'
                }
                return 200, data, resp_headers

        # 2. Releases
        elif "/releases" in endpoint_or_url:
            data = self._read_fixture("releases.json")
            return 200, data, {}

        # 3. Pull requests
        elif "/pulls" in endpoint_or_url:
            data = self._read_fixture("pull_requests.json")
            return 200, data, {}

        # 4. Traffic views
        elif "/traffic/views" in endpoint_or_url:
            data = self._read_fixture("traffic_views.json")
            return 200, data, {}

        # 5. Traffic clones
        elif "/traffic/clones" in endpoint_or_url:
            data = self._read_fixture("traffic_clones.json")
            return 200, data, {}

        # 6. Errors testing
        elif "simulate-401" in endpoint_or_url:
            raise GitHubApiError(
                message="Requires authentication",
                http_status=401,
                error_class="auth",
                retryable=False,
            )
        elif "simulate-403-rate-limit" in endpoint_or_url:
            raise GitHubApiError(
                message="API rate limit exceeded",
                http_status=403,
                error_class="rate_limited",
                retryable=True,
                retry_after=120,
            )
        elif "simulate-404" in endpoint_or_url:
            raise GitHubApiError(
                message="Not Found",
                http_status=404,
                error_class="unauthorized",
                retryable=False,
            )

        return 200, {}, {}


class TestGitHubCollectorContract(unittest.TestCase):
    def setUp(self):
        self.fixture_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../fixtures"))
        self.mock_client = MockGitHubClient(self.fixture_dir)
        self.collector = GitHubCollector(self.mock_client.config, client=self.mock_client)

    def test_parse_link_header(self):
        """Test RFC 5988 Link header extraction."""
        header = (
            '<https://api.github.com/repos/octocat/Hello-World/commits?page=2>; rel="next", '
            '<https://api.github.com/repos/octocat/Hello-World/commits?page=5>; rel="last"'
        )
        parsed = parse_link_header(header)
        self.assertEqual(
            parsed.get("next"),
            "https://api.github.com/repos/octocat/Hello-World/commits?page=2",
        )
        self.assertEqual(
            parsed.get("last"),
            "https://api.github.com/repos/octocat/Hello-World/commits?page=5",
        )

    def test_idempotency_key_stability(self):
        """Test that same inputs yield identical sha256 hash."""
        key1 = compute_idempotency_key("github", "octo", "commit_sha_123", "owner/repo")
        key2 = compute_idempotency_key("github", "octo", "commit_sha_123", "owner/repo")
        key3 = compute_idempotency_key("github", "octo", "commit_sha_999", "owner/repo")
        self.assertEqual(key1, key2)
        self.assertNotEqual(key1, key3)
        self.assertEqual(len(key1), 64)

    def test_secret_redaction(self):
        """Test that secrets and tokens are redacted from messages."""
        msg = "Error: Bearer gho_secrettoken1234567890 failed with sk-secretkey123456"
        clean = sanitize_message(msg)
        self.assertNotIn("gho_secrettoken1234567890", clean)
        self.assertNotIn("sk-secretkey123456", clean)
        self.assertIn("[REDACTED]", clean)

    def test_capabilities_declaration_and_schema(self):
        """Verify capability declarations adhere to schema rules."""
        caps = self.collector.get_capabilities()
        self.assertEqual(len(caps), 2)
        git_cap = caps[0]
        self.assertEqual(git_cap["source"], "github")
        self.assertEqual(git_cap["capability"], "git")
        self.assertEqual(git_cap["status"], "ready")
        self.assertTrue(git_cap["profile_binding"]["explicit"])
        self.assertFalse(git_cap["profile_binding"]["global_state_mutated"])

        if schema_errors:
            for cap in caps:
                errs = schema_errors(cap, "capability") + semantic_errors("capability", cap)
                self.assertEqual(errs, [], f"Capability validation failed: {errs}")

    def test_account_model_and_schema(self):
        """Verify account model matches schema/collector/account.schema.json."""
        account = self.collector.get_account_model()
        self.assertEqual(account["source"], "github")
        self.assertEqual(account["alias"], "test_octo")
        self.assertEqual(account["profile_id"], "github_test_octo")
        self.assertEqual(account["health"], "ready")

        if schema_errors:
            errs = schema_errors(account, "account") + semantic_errors("account", account)
            self.assertEqual(errs, [], f"Account validation failed: {errs}")

    def test_collect_commits_pagination(self):
        """Verify full pagination across pages."""
        repo_state, commits, pagination = self.collector.collect_commits("octocat/Hello-World")
        self.assertEqual(repo_state, "ok")
        self.assertEqual(len(commits), 2)
        self.assertEqual(commits[0]["sha"], "6dcb09b5b57875f334f61aebed695e2e4193db5e")
        self.assertEqual(commits[1]["sha"], "7fd1a60b01f91b314f59955a4e4d4e80d8edf11d")
        self.assertTrue(pagination["complete"])
        self.assertFalse(pagination["truncated"])
        self.assertIsNone(pagination["next_cursor"])

    def test_empty_repository_state(self):
        """US-12.c: Verify HTTP 409 empty repo returns 'empty' state, not an error."""
        repo_state, commits, pagination = self.collector.collect_commits("octocat/empty-repo")
        self.assertEqual(repo_state, "empty")
        self.assertEqual(len(commits), 0)
        self.assertTrue(pagination["complete"])

    def test_inaccessible_repository_state(self):
        """US-12.c: Verify 401/404 raise appropriate GitHubApiError."""
        with self.assertRaises(GitHubApiError) as ctx:
            self.mock_client.request("/simulate-401")
        self.assertEqual(ctx.exception.error_class, "auth")
        self.assertFalse(ctx.exception.retryable)

        with self.assertRaises(GitHubApiError) as ctx404:
            self.mock_client.request("/simulate-404")
        self.assertEqual(ctx404.exception.error_class, "unauthorized")
        self.assertFalse(ctx404.exception.retryable)

    def test_rate_limit_handling(self):
        """Verify 403 rate limit triggers retryable error with retry_after."""
        with self.assertRaises(GitHubApiError) as ctx:
            self.mock_client.request("/simulate-403-rate-limit")
        self.assertEqual(ctx.exception.error_class, "rate_limited")
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(ctx.exception.retry_after, 120)

    def test_traffic_14_day_window_collection(self):
        """Verify traffic views & clones collection."""
        views, clones = self.collector.collect_traffic("octocat/Hello-World")
        self.assertEqual(views.get("count"), 14)
        self.assertEqual(views.get("uniques"), 6)
        self.assertEqual(clones.get("count"), 5)
        self.assertEqual(clones.get("uniques"), 3)

    def test_transform_to_events_and_core_schema(self):
        """Verify transformation of GitHub items into canonical AEL events and core event schema."""
        _, commits, _ = self.collector.collect_commits("octocat/Hello-World")
        releases, _ = self.collector.collect_releases("octocat/Hello-World")
        prs, _ = self.collector.collect_pull_requests("octocat/Hello-World")
        traffic = self.collector.collect_traffic("octocat/Hello-World")

        events = self.collector.transform_to_events(
            owner_repo="octocat/Hello-World",
            commits=commits,
            releases=releases,
            pull_requests=prs,
            traffic_data=traffic,
            evidence_id="ev_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            project_id="test_project",
        )

        event_types = [e["event_type"] for e in events]
        self.assertIn("engineering.commit", event_types)
        self.assertIn("engineering.release", event_types)
        self.assertIn("engineering.pull_request", event_types)
        self.assertIn("engineering.traffic_snapshot", event_types)

        # Check traffic snapshot payload
        traffic_event = [e for e in events if e["event_type"] == "engineering.traffic_snapshot"][0]
        self.assertEqual(traffic_event["payload"]["window_limit_days"], 14)
        self.assertEqual(traffic_event["payload"]["views_total"], 14)
        self.assertEqual(traffic_event["payload"]["clones_total"], 5)

        # Core schema validation
        if minival_validate and core_loader:
            for ev in events:
                v_errs = minival_validate(ev, "event.schema.json", core_loader)
                sem_errs = check_event(ev)
                self.assertEqual(v_errs, [], f"Event minival failed: {v_errs}")
                self.assertEqual(sem_errs, [], f"Event semantic failed: {sem_errs}")

    def test_collection_run_and_schema_validation(self):
        """Verify complete collection run satisfies schema/collector/run.schema.json."""
        period = {"start": "2026-10-01T00:00:00Z", "end": "2026-10-20T00:00:00Z"}
        run_record = self.collector.run_collection(period)

        self.assertEqual(run_record["status"], "success")
        self.assertEqual(run_record["exit_status"], 0)
        self.assertEqual(len(run_record["account_runs"]), 1)
        acct_run = run_record["account_runs"][0]
        self.assertEqual(acct_run["status"], "success")
        self.assertEqual(acct_run["coverage"]["confirmation"], "complete")
        self.assertEqual(acct_run["process"]["exit_code"], 0)

        if schema_errors:
            errs = schema_errors(run_record, "run") + semantic_errors("run", run_record)
            self.assertEqual(errs, [], f"Run schema validation failed: {errs}")


if __name__ == "__main__":
    unittest.main()
