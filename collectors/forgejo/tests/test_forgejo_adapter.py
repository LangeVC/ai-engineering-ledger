"""
Unit and Contract tests for Forgejo Collector Adapter.
Tests:
- Link header parsing and pagination mechanics
- Account, capabilities, and profile binding schema conformity
- Idempotency and entity extraction
- Handling of empty repository (US-12.c) vs inaccessible / 404 / 128
- Local Git SSH fallback when Forgejo API has no token (US-12.b)
- Verification that local Git tags are NOT classified as forge releases (US-12.b)
- D01 compliance: candidate equivalence without assuming canonical role
- Execution of collector contract semantic validator against produced records
"""

import copy
import json
import os
import tempfile
import unittest
import subprocess
from datetime import datetime, timezone

from collectors.forgejo.adapter import (
    ForgejoAccountConfig,
    ForgejoAdapter,
    ForgejoRepoConfig,
    generate_idempotency_key
)
from collectors.forgejo.client import ForgejoApiClient, ForgejoApiError, GitSSHClient
from collectors.forgejo.pagination import parse_link_header, extract_page_param


FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "..", "fixtures")


class MockForgejoApiClient(ForgejoApiClient):
    """Mock API client returning static synthetic fixtures."""

    def __init__(self, should_fail_403: bool = False):
        super().__init__(base_url="https://mock.forgejo.local")
        self.should_fail_403 = should_fail_403

    def request(self, endpoint: str, params: dict = None):
        if self.should_fail_403:
            raise ForgejoApiError("Forbidden", status_code=403, body='{"message":"Only signed in user is allowed to call APIs."}')

        if "commits" in endpoint:
            with open(os.path.join(FIXTURES_DIR, "api_commits_page1.json")) as f:
                data = json.load(f)
            headers = {"Link": '<https://mock.forgejo.local/api/v1/repos/o/r/commits?page=2>; rel="next"'} if (params and params.get("page", 1) == 1) else {}
            # Return empty on page 2 to terminate pagination
            if params and params.get("page") == 2:
                return [], {}, 200
            return data, headers, 200

        if "releases" in endpoint:
            with open(os.path.join(FIXTURES_DIR, "api_releases.json")) as f:
                data = json.load(f)
            return data, {}, 200

        if "pulls" in endpoint:
            with open(os.path.join(FIXTURES_DIR, "api_pull_requests.json")) as f:
                data = json.load(f)
            return data, {}, 200

        if endpoint.startswith("repos/"):
            with open(os.path.join(FIXTURES_DIR, "api_repo_empty.json")) as f:
                data = json.load(f)
            return data, {}, 200

        raise ForgejoApiError(f"Unknown endpoint: {endpoint}", status_code=404)


class MockGitSSHClient(GitSSHClient):
    """Mock Git client returning controlled remote & local inspection states."""

    def __init__(self, empty: bool = False, not_found: bool = False, local_commits: list = None, local_tags: list = None):
        super().__init__(host="mock.git.local")
        self.empty = empty
        self.not_found = not_found
        self.local_commits = local_commits or []
        self.local_tags = local_tags or []

    def check_ssh_access(self):
        return {"authenticated": True, "raw_output": "Hi there! Forgejo does not provide shell access.", "returncode": 0}

    def inspect_remote_refs(self, remote_url: str):
        if self.not_found:
            return {"returncode": 128, "is_empty": False, "is_not_found": True, "refs": [], "stderr": "Cannot find repository"}
        if self.empty:
            return {"returncode": 2, "is_empty": True, "is_not_found": False, "refs": [], "stderr": ""}
        return {"returncode": 0, "is_empty": False, "is_not_found": False, "refs": [{"sha": "a1b2c3d4", "ref": "refs/heads/main"}], "stderr": ""}

    def inspect_local_git_repo(self, repo_dir: str):
        return {
            "exists": True,
            "commits": self.local_commits,
            "tags": self.local_tags
        }


class TestPagination(unittest.TestCase):
    def test_parse_link_header(self):
        header = '<https://forge.example.invalid/api/v1/repos/o/r/commits?page=2&limit=50>; rel="next", <https://forge.example.invalid/api/v1/repos/o/r/commits?page=10&limit=50>; rel="last"'
        links = parse_link_header(header)
        self.assertEqual(links.get("next"), "https://forge.example.invalid/api/v1/repos/o/r/commits?page=2&limit=50")
        self.assertEqual(links.get("last"), "https://forge.example.invalid/api/v1/repos/o/r/commits?page=10&limit=50")

    def test_extract_page_param(self):
        url = "https://forge.example.invalid/api/v1/repos/o/r/commits?page=42&limit=50"
        self.assertEqual(extract_page_param(url), 42)


class TestForgejoAdapterContract(unittest.TestCase):
    def setUp(self):
        self.account = ForgejoAccountConfig(
            alias="example-account",
            profile_id="forgejo_default",
            host="forge.example.invalid",
            base_url="https://forge.example.invalid"
        )
        self.repo = ForgejoRepoConfig(
            owner="example-org",
            name="example-repo"
        )

    def test_capabilities_declaration_unverified_api_and_ready_ssh(self):
        """PRD US-12.b & D01: Unverified API token must yield status 'unverified' with reason."""
        mock_git = MockGitSSHClient(empty=True)
        adapter = ForgejoAdapter(
            account_config=self.account,
            repo_configs=[self.repo],
            git_client=mock_git
        )
        caps = adapter.assess_capabilities()
        self.assertEqual(len(caps), 2)

        # 1. Git SSH cap
        git_cap = next(c for c in caps if c["capability"] == "git")
        self.assertEqual(git_cap["status"], "ready")
        self.assertTrue(git_cap["profile_binding"]["explicit"])
        self.assertFalse(git_cap["profile_binding"]["global_state_mutated"])

        # 2. Historical API cap
        hist_cap = next(c for c in caps if c["capability"] == "historical")
        self.assertEqual(hist_cap["status"], "unverified")
        self.assertIn("reason", hist_cap)
        self.assertIn("token", hist_cap["reason"].lower())

    def test_empty_repo_distinction_us12c(self):
        """PRD US-12.c: Empty repository returns evidenced_zero observation."""
        mock_git = MockGitSSHClient(empty=True)
        adapter = ForgejoAdapter(
            account_config=self.account,
            repo_configs=[self.repo],
            git_client=mock_git
        )
        result = adapter.collect()
        empty_obs = next((o for o in result.observations if o.get("state") == "evidenced_zero"), None)
        self.assertIsNotNone(empty_obs)
        self.assertEqual(empty_obs["value"], 0)
        self.assertEqual(empty_obs["unit"], "commits")

    def test_inaccessible_repo_distinction_us12c(self):
        """PRD US-12.c: Inaccessible repository returns distinct unauthorized observation and error."""
        mock_git = MockGitSSHClient(not_found=True)
        adapter = ForgejoAdapter(
            account_config=self.account,
            repo_configs=[self.repo],
            git_client=mock_git
        )
        result = adapter.collect()
        self.assertEqual(result.status, "failure")
        self.assertEqual(result.exit_status, 20)
        unauth_obs = next((o for o in result.observations if o.get("state") == "unauthorized"), None)
        self.assertIsNotNone(unauth_obs)
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.errors[0]["error_class"], "unauthorized")

    def test_api_collection_with_pagination_and_events(self):
        """When API token is present, full pagination and entity extraction runs."""
        account_with_token = ForgejoAccountConfig(
            alias="example-account",
            profile_id="forgejo_default",
            api_token="test_mock_token"
        )
        mock_api = MockForgejoApiClient()
        mock_git = MockGitSSHClient(empty=False)
        adapter = ForgejoAdapter(
            account_config=account_with_token,
            repo_configs=[self.repo],
            api_client=mock_api,
            git_client=mock_git
        )
        result = adapter.collect()
        self.assertEqual(result.status, "success")
        self.assertEqual(result.exit_status, 0)
        self.assertGreater(len(result.events), 0)

        # Check commit event
        commit_evt = next((e for e in result.events if e["event_type"] == "engineering.commit"), None)
        self.assertIsNotNone(commit_evt)
        self.assertEqual(commit_evt["source"], "forgejo")
        self.assertEqual(commit_evt["provenance"]["class"], "P0")

        # Check release event
        rel_evt = next((e for e in result.events if e["event_type"] == "engineering.release"), None)
        self.assertIsNotNone(rel_evt)
        self.assertEqual(rel_evt["event_type"], "engineering.release")

        # Check pull request event
        pr_evt = next((e for e in result.events if e["event_type"] == "engineering.pull_request"), None)
        self.assertIsNotNone(pr_evt)
        self.assertEqual(pr_evt["event_type"], "engineering.pull_request")

    def test_git_fallback_no_fake_releases_us12b(self):
        """PRD US-12.b: In Git fallback, tags are NOT classified or emitted as Forge releases."""
        mock_git = MockGitSSHClient(
            empty=False,
            local_commits=[{
                "sha": "1234567890123456789012345678901234567890",
                "author": {"name": "Test", "email": "test@local", "date": "2026-10-20T10:00:00Z"},
                "committer": {"name": "Test", "email": "test@local", "date": "2026-10-20T10:00:00Z"},
                "message": "local commit"
            }],
            local_tags=[{"name": "v0.1.0", "sha": "1234567890123456789012345678901234567890"}]
        )
        repo_with_local = ForgejoRepoConfig(
            owner="example-org",
            name="example-repo",
            local_path="/mock/path/repo"
        )
        # Mock existence check for local repo
        orig_exists = os.path.exists
        try:
            os.path.exists = lambda p: True if p == "/mock/path/repo" else orig_exists(p)
            adapter = ForgejoAdapter(
                account_config=self.account,  # no token
                repo_configs=[repo_with_local],
                git_client=mock_git
            )
            result = adapter.collect()
            # Assert NO engineering.release event emitted
            release_events = [e for e in result.events if e["event_type"] == "engineering.release"]
            self.assertEqual(len(release_events), 0, "Local Git tags must not be emitted as releases")

            # Check commit event exists with P1 provenance
            commit_evt = next((e for e in result.events if e["event_type"] == "engineering.commit"), None)
            self.assertIsNotNone(commit_evt)
            self.assertEqual(commit_evt["provenance"]["class"], "P1")
            self.assertEqual(commit_evt["provenance"]["method"], "local")

            # Assert unverified observation recorded for PR/Releases
            unverified_obs = next((o for o in result.observations if o["state"] == "unverified"), None)
            self.assertIsNotNone(unverified_obs)
        finally:
            os.path.exists = orig_exists

    def test_idempotency_key_stability(self):
        """Verify DQ03 stable sha256 idempotency key generation."""
        k1 = generate_idempotency_key("forgejo", "example-account", "commit", "sha_abc_123")
        k2 = generate_idempotency_key("forgejo", "example-account", "commit", "sha_abc_123")
        self.assertEqual(k1, k2)
        self.assertEqual(len(k1), 64)

    def test_live_hold_declaration_on_real_instance(self):
        """
        Optional integration check with a user-configured live Forgejo instance:
        Should confirm SSH access or report clean Live-HOLD without crashing or leaking secrets.
        """
        host = os.environ.get("AEL_LIVE_FORGEJO_HOST")
        if not host:
            self.skipTest("AEL_LIVE_FORGEJO_HOST is not configured")
        real_client = GitSSHClient(host=host)
        ssh_res = real_client.check_ssh_access()
        self.assertIn("raw_output", ssh_res)
        self.assertNotIn("secret", ssh_res["raw_output"].lower())

    def test_collector_and_event_schema_contract_compliance(self):
        """
        Validates produced run_record, capabilities, observations, and events
        against repository JSON Schemas and semantic validators.
        """
        # Find schema dir
        cur = os.path.abspath(os.path.dirname(__file__))
        repo_root = None
        while cur != os.path.dirname(cur):
            if os.path.exists(os.path.join(cur, "schema", "collector")):
                repo_root = cur
                break
            cur = os.path.dirname(cur)

        if not repo_root:
            # Fallback to known relative location in worktree context
            parent_repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
            if os.path.exists(os.path.join(parent_repo, "schema", "collector")):
                repo_root = parent_repo

        if not repo_root:
            self.skipTest("Schema directory not found for contract test")

        import sys
        collector_test_dir = os.path.join(repo_root, "schema", "collector", "tests")
        core_dir = os.path.join(repo_root, "schema", "core")
        core_test_dir = os.path.join(core_dir, "tests")

        if collector_test_dir not in sys.path:
            sys.path.insert(0, collector_test_dir)
        if core_dir not in sys.path:
            sys.path.insert(0, core_dir)
        if core_test_dir not in sys.path:
            sys.path.insert(0, core_test_dir)

        import validate as collector_validate
        import minival
        import semantic as core_semantic

        # 1. Run collection with mock api client
        account_with_token = ForgejoAccountConfig(
            alias="example-account",
            profile_id="forgejo_default",
            api_token="test_mock_token"
        )
        mock_api = MockForgejoApiClient()
        mock_git = MockGitSSHClient(empty=False)
        adapter = ForgejoAdapter(
            account_config=account_with_token,
            repo_configs=[self.repo],
            api_client=mock_api,
            git_client=mock_git
        )
        result = adapter.collect()

        # Validate run_record
        run_rec = result.run_record
        errs = collector_validate.schema_errors(run_rec, "run")
        self.assertEqual(errs, [], f"run_record schema errors: {errs}")
        sem_errs = collector_validate.semantic_errors("run", run_rec)
        self.assertEqual(sem_errs, [], f"run_record semantic errors: {sem_errs}")

        # Validate capabilities
        for cap in result.capabilities:
            errs = collector_validate.schema_errors(cap, "capability")
            self.assertEqual(errs, [], f"capability schema errors: {errs}")

        # Validate observations
        for obs in result.observations:
            errs = collector_validate.schema_errors(obs, "observation")
            self.assertEqual(errs, [], f"observation schema errors: {errs}")

        # Validate core events
        loader = minival.Loader(core_dir)
        for ev in result.events:
            errs = minival.validate(ev, "event.schema.json", loader)
            self.assertEqual(errs, [], f"event schema errors: {errs}")
            sem_errs = core_semantic.check_event(ev)
            self.assertEqual(sem_errs, [], f"event semantic errors: {sem_errs}")


if __name__ == "__main__":
    unittest.main()
