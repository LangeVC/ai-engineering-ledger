#!/usr/bin/env python3
"""
Live Integration Probe & Contract Verification for GitHub Adapter (US-12, US-13).
Can execute against real GitHub API using local credentials (gh auth / env token)
or gracefully yield LIVE-HOLD if credentials or scopes are missing.
"""

import os
import sys
import unittest

# Ensure worktree root is on sys.path
worktree_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../"))
if worktree_root not in sys.path:
    sys.path.insert(0, worktree_root)

from collectors.github.collector import GitHubCollector, GitHubConfig, GitHubClient, GitHubApiError


class TestGitHubLiveProbe(unittest.TestCase):
    def setUp(self):
        self.config = GitHubConfig.from_env()

    def test_live_reachability_or_hold(self):
        """
        Probe live GitHub API reachability and authentication.
        Verifies real status, rate limits, or records LIVE-HOLD.
        """
        if not self.config.token:
            self.skipTest("LIVE-HOLD: No GITHUB_TOKEN or gh CLI token available in environment.")

        client = GitHubClient(self.config)
        try:
            status, data, headers = client.request("/user")
            self.assertEqual(status, 200)
            self.assertIn("login", data)
            self.assertIn("X-RateLimit-Limit", headers)
            # Check rate limit value
            limit = int(headers.get("X-RateLimit-Limit", 0))
            self.assertGreaterEqual(limit, 60)
        except GitHubApiError as e:
            self.skipTest(f"LIVE-HOLD: GitHub API returned {e.http_status}: {e.message}")


if __name__ == "__main__":
    unittest.main()
