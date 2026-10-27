"""
Forgejo API and Git SSH clients using Python 3 standard library.
"""

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from .pagination import parse_link_header


class ForgejoApiError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None, body: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class ForgejoApiClient:
    """
    Client for Forgejo REST API v1.
    Handles authentication via Bearer token (or Token header), pagination, and status inspection.
    """

    def __init__(
        self,
        base_url: str,
        token: Optional[str] = None,
        timeout_seconds: float = 10.0,
        user_agent: str = "ael-forgejo-collector/0.1"
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent

    def _build_request(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> urllib.request.Request:
        url = endpoint if endpoint.startswith("http") else f"{self.base_url}/api/v1/{endpoint.lstrip('/')}"
        if params:
            query = urllib.parse.urlencode({k: str(v) for k, v in params.items() if v is not None})
            delimiter = "&" if "?" in url else "?"
            url = f"{url}{delimiter}{query}"

        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json"
        }
        if self.token:
            # Forgejo supports both 'token <token>' and 'Bearer <token>'
            headers["Authorization"] = f"token {self.token}"

        return urllib.request.Request(url, headers=headers)

    def request(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Tuple[Any, Dict[str, str], int]:
        """
        Executes a GET request against Forgejo API.
        Returns: (parsed_json_body, response_headers, http_status_code)
        """
        req = self._build_request(endpoint, params)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                status = resp.status
                headers = dict(resp.headers)
                raw_bytes = resp.read()
                data = json.loads(raw_bytes.decode("utf-8")) if raw_bytes else None
                return data, headers, status
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise ForgejoApiError(f"Forgejo API returned HTTP {e.code}", status_code=e.code, body=body) from e
        except urllib.error.URLError as e:
            raise ForgejoApiError(f"Forgejo API network connection error: {e.reason}") from e

    def get_repo(self, owner: str, repo: str) -> Dict[str, Any]:
        """Get repository metadata."""
        data, _, _ = self.request(f"repos/{owner}/{repo}")
        return data

    def list_commits(self, owner: str, repo: str, page: int = 1, limit: int = 50) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
        """List repository commits with pagination."""
        data, headers, _ = self.request(f"repos/{owner}/{repo}/commits", params={"page": page, "limit": limit})
        return data or [], headers

    def list_releases(self, owner: str, repo: str, page: int = 1, limit: int = 50) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
        """List repository releases with pagination."""
        data, headers, _ = self.request(f"repos/{owner}/{repo}/releases", params={"page": page, "limit": limit})
        return data or [], headers

    def list_pull_requests(self, owner: str, repo: str, state: str = "all", page: int = 1, limit: int = 50) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
        """List repository pull requests with pagination."""
        data, headers, _ = self.request(f"repos/{owner}/{repo}/pulls", params={"state": state, "page": page, "limit": limit})
        return data or [], headers


class GitSSHClient:
    """
    Fallback client interacting with Forgejo via local Git CLI / SSH protocol.
    Used when Forgejo API is unverified / lacks credentials (PRD US-12.b).
    """

    def __init__(self, host: str = "forge.example.invalid", connect_timeout: int = 5):
        self.host = host
        self.connect_timeout = connect_timeout

    def check_ssh_access(self) -> Dict[str, Any]:
        """
        Tests SSH handshake with git@<host>.
        """
        cmd = ["ssh", "-T", "-o", f"ConnectTimeout={self.connect_timeout}", "-o", "BatchMode=yes", f"git@{self.host}"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        combined = proc.stdout + proc.stderr
        authenticated = "Hi there" in combined and "Forgejo does not provide shell access" in combined
        return {
            "authenticated": authenticated,
            "raw_output": combined.strip(),
            "returncode": proc.returncode
        }

    def inspect_remote_refs(self, remote_url: str) -> Dict[str, Any]:
        """
        Calls git ls-remote to check repository existence and ref count.
        - Exit code 0: repository exists and has refs.
        - Exit code 2: repository exists but is completely empty (zero refs).
        - Exit code 128: repository not found, or unauthorized.
        """
        cmd = ["git", "ls-remote", "--exit-code", remote_url]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        is_empty = proc.returncode == 2
        is_not_found = proc.returncode == 128
        refs = []
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                parts = line.strip().split("\t")
                if len(parts) == 2:
                    refs.append({"sha": parts[0], "ref": parts[1]})

        return {
            "returncode": proc.returncode,
            "is_empty": is_empty,
            "is_not_found": is_not_found,
            "refs": refs,
            "stderr": proc.stderr.strip()
        }

    def inspect_local_git_repo(self, repo_dir: str) -> Dict[str, Any]:
        """
        Extracts commits, branch references, and tags from a local Git worktree/clone.
        Conforming to US-12.b: Git tags MUST NOT be classified as forge releases.
        """
        if not os.path.exists(os.path.join(repo_dir, ".git")):
            return {"exists": False, "commits": [], "tags": [], "branches": []}

        # Commits
        cmd_commits = [
            "git", "-C", repo_dir, "log",
            "--format=%H%x00%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI%x00%s"
        ]
        proc_commits = subprocess.run(cmd_commits, capture_output=True, text=True)
        commits = []
        if proc_commits.returncode == 0 and proc_commits.stdout.strip():
            for line in proc_commits.stdout.strip().splitlines():
                fields = line.split("\x00")
                if len(fields) >= 8:
                    commits.append({
                        "sha": fields[0],
                        "author": {"name": fields[1], "email": fields[2], "date": fields[3]},
                        "committer": {"name": fields[4], "email": fields[5], "date": fields[6]},
                        "message": fields[7]
                    })

        # Tags (strictly local git tags, NOT releases)
        cmd_tags = ["git", "-C", repo_dir, "tag", "-l", "--format=%(refname:short)%x00%(objectname)"]
        proc_tags = subprocess.run(cmd_tags, capture_output=True, text=True)
        tags = []
        if proc_tags.returncode == 0 and proc_tags.stdout.strip():
            for line in proc_tags.stdout.strip().splitlines():
                parts = line.split("\x00")
                if len(parts) == 2:
                    tags.append({"name": parts[0], "sha": parts[1]})

        return {
            "exists": True,
            "commits": commits,
            "tags": tags
        }
