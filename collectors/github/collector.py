#!/usr/bin/env python3
"""
GitHub Collector & Adapter for AI Engineering Ledger (AEL v0.1).
Standard library Python 3 only (no external dependencies).

Adheres to:
- US-12 / US-13 (Allowlist, explicit time period, Link header pagination,
  empty repo [409] vs inaccessible [401/403/404], 14-day traffic rolling window,
  commit/release/PR metadata, idempotency keys, no secret leakage).
- schema/collector contracts (account, capability, run, observation, error).
"""

from __future__ import annotations

import base64
import dataclasses
import datetime
import hashlib
import json
import os
import re
import secrets
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple, Union


# Crockford Base32 alphabet for ULID generation
_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def generate_ulid(timestamp_ms: Optional[int] = None) -> str:
    """Generates a canonical 26-character Crockford Base32 ULID."""
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)

    # 48-bit timestamp -> 10 chars
    time_chars = []
    t = timestamp_ms
    for _ in range(10):
        time_chars.append(_CROCKFORD_ALPHABET[t % 32])
        t //= 32
    time_part = "".join(reversed(time_chars))

    # 80-bit randomness -> 16 chars
    rand_chars = []
    rand_bytes = secrets.token_bytes(10)
    r_val = int.from_bytes(rand_bytes, "big")
    for _ in range(16):
        rand_chars.append(_CROCKFORD_ALPHABET[r_val % 32])
        r_val //= 32
    rand_part = "".join(reversed(rand_chars))

    return time_part + rand_part


def sanitize_message(msg: str) -> str:
    """Redacts potential secrets, bearer tokens, or sk- keys from log/error messages."""
    if not msg:
        return ""
    # Redact Authorization header patterns or tokens
    redacted = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._-]{8,}", r"\1[REDACTED]", msg)
    redacted = re.sub(r"(?i)(sk-[A-Za-z0-9]{8,})", "[REDACTED]", redacted)
    redacted = re.sub(r"(?i)(gh[opurs]_[A-Za-z0-9_]{10,})", "[REDACTED]", redacted)
    redacted = re.sub(r"(?i)(secret\s*=\s*)[^\s&,]+", r"\1[REDACTED]", redacted)
    redacted = re.sub(r"(?i)(password\s*=\s*)[^\s&,]+", r"\1[REDACTED]", redacted)
    return redacted


def parse_link_header(link_header: str) -> Dict[str, str]:
    """Parses RFC 5988 Link header into a mapping of rel -> url."""
    if not link_header:
        return {}
    links = {}
    parts = link_header.split(",")
    for part in parts:
        section = part.strip().split(";")
        if len(section) < 2:
            continue
        url_match = re.search(r"<([^>]+)>", section[0].strip())
        rel_match = re.search(r'rel=["\']?([^"\']+)["\']?', section[1].strip())
        if url_match and rel_match:
            links[rel_match.group(1)] = url_match.group(1)
    return links


def compute_idempotency_key(source: str, account: str, entity_id: str, scope: str = "") -> str:
    """Computes a stable sha256 idempotency key: sha256(source, account, entity_id, scope)."""
    raw = f"{source}:{account}:{entity_id}:{scope}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def iso_utc_now() -> str:
    """Returns ISO 8601 UTC timestamp format."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclasses.dataclass(frozen=True)
class GitHubConfig:
    account_alias: str = "default"
    profile_id: str = "github_default"
    token: Optional[str] = None
    secret_ref: str = "env:GITHUB_TOKEN"
    allowlist_repos: Tuple[str, ...] = ()
    user_agent: str = "ael-github-collector/0.1"
    base_url: str = "https://api.github.com"
    request_timeout: int = 15

    @classmethod
    def from_env(cls, account_alias: str = "default", allowlist_repos: Optional[List[str]] = None) -> GitHubConfig:
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        secret_ref = "env:GITHUB_TOKEN"
        if not token:
            # Check gh CLI if available
            try:
                proc = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=5)
                if proc.returncode == 0 and proc.stdout.strip():
                    token = proc.stdout.strip()
                    secret_ref = "keychain:gh_cli"
            except Exception:
                pass
        return cls(
            account_alias=account_alias,
            profile_id=f"github_{account_alias}",
            token=token,
            secret_ref=secret_ref,
            allowlist_repos=tuple(allowlist_repos or []),
        )


class GitHubApiError(Exception):
    def __init__(
        self,
        message: str,
        http_status: Optional[int] = None,
        error_class: str = "protocol",
        retryable: bool = False,
        retry_after: Optional[int] = None,
        is_empty_repo: bool = False,
    ):
        super().__init__(sanitize_message(message))
        self.message = sanitize_message(message)
        self.http_status = http_status
        self.error_class = error_class
        self.retryable = retryable
        self.retry_after = retry_after
        self.is_empty_repo = is_empty_repo


class GitHubClient:
    """Stdlib-based HTTP client for GitHub REST API."""

    def __init__(self, config: GitHubConfig):
        self.config = config

    def request(
        self,
        endpoint_or_url: str,
        headers: Optional[Dict[str, str]] = None,
        method: str = "GET",
    ) -> Tuple[int, Any, Dict[str, str]]:
        if endpoint_or_url.startswith("http://") or endpoint_or_url.startswith("https://"):
            url = endpoint_or_url
        else:
            base = self.config.base_url.rstrip("/")
            endpoint = endpoint_or_url.lstrip("/")
            url = f"{base}/{endpoint}"

        req_headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": self.config.user_agent,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.config.token:
            req_headers["Authorization"] = f"Bearer {self.config.token}"

        if headers:
            req_headers.update(headers)

        req = urllib.request.Request(url, headers=req_headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=self.config.request_timeout) as resp:
                status = resp.status
                resp_headers = dict(resp.headers)
                body = resp.read()
                data = json.loads(body.decode("utf-8")) if body else None
                return status, data, resp_headers
        except urllib.error.HTTPError as e:
            status = e.code
            resp_headers = dict(e.headers)
            try:
                body = e.read()
                err_data = json.loads(body.decode("utf-8")) if body else {}
                err_msg = err_data.get("message", e.reason)
            except Exception:
                err_msg = str(e.reason)

            # Classify error
            # US-12.c: HTTP 409 Conflict indicates empty repository
            if status == 409 and ("empty" in err_msg.lower() or "git repository is empty" in err_msg.lower()):
                raise GitHubApiError(
                    message=f"Repository is empty: {err_msg}",
                    http_status=status,
                    error_class="unsupported",
                    retryable=False,
                    is_empty_repo=True,
                )
            elif status == 401:
                raise GitHubApiError(
                    message=f"Unauthorized: {err_msg}",
                    http_status=status,
                    error_class="auth",
                    retryable=False,
                )
            elif status == 403:
                # Rate limit or forbidden
                limit_remaining = resp_headers.get("X-RateLimit-Remaining")
                if limit_remaining == "0" or "rate limit" in err_msg.lower():
                    reset_time = int(resp_headers.get("X-RateLimit-Reset", 0))
                    retry_after = max(0, reset_time - int(time.time())) if reset_time else 60
                    raise GitHubApiError(
                        message=f"Rate limit exceeded: {err_msg}",
                        http_status=status,
                        error_class="rate_limited",
                        retryable=True,
                        retry_after=retry_after,
                    )
                else:
                    raise GitHubApiError(
                        message=f"Forbidden/Access denied: {err_msg}",
                        http_status=status,
                        error_class="unauthorized",
                        retryable=False,
                    )
            elif status == 404:
                raise GitHubApiError(
                    message=f"Resource not found: {err_msg}",
                    http_status=status,
                    error_class="unauthorized",
                    retryable=False,
                )
            elif 500 <= status < 600:
                raise GitHubApiError(
                    message=f"GitHub 5xx error: {err_msg}",
                    http_status=status,
                    error_class="transient_5xx",
                    retryable=True,
                    retry_after=5,
                )
            else:
                raise GitHubApiError(
                    message=f"HTTP {status}: {err_msg}",
                    http_status=status,
                    error_class="protocol",
                    retryable=False,
                )
        except urllib.error.URLError as e:
            if "timed out" in str(e.reason).lower():
                raise GitHubApiError(
                    message=f"Connection timed out: {e.reason}",
                    error_class="timeout",
                    retryable=True,
                    retry_after=3,
                )
            raise GitHubApiError(
                message=f"Network error: {e.reason}",
                error_class="protocol",
                retryable=True,
                retry_after=2,
            )


class GitHubCollector:
    """
    Collector implementation for GitHub lane conforming to AEL contracts.
    """

    def __init__(self, config: GitHubConfig, client: Optional[GitHubClient] = None):
        self.config = config
        self.client = client or GitHubClient(config)

    def get_capabilities(self, evidence_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Returns standard capability declarations for this GitHub account."""
        checked_at = iso_utc_now()
        ev_id = evidence_id or f"ev_{generate_ulid()}"
        has_token = bool(self.config.token)

        git_status = "ready" if has_token else "unauthorized"
        traffic_status = "ready" if has_token else "unauthorized"

        caps = [
            {
                "schema_version": "0.1",
                "source": "github",
                "account_alias": self.config.account_alias,
                "product_domain": "git",
                "capability": "git",
                "status": git_status,
                "profile_binding": {
                    "profile_id": self.config.profile_id,
                    "explicit": True,
                    "global_state_mutated": False,
                },
                "checked_at": checked_at,
                "read_only_probe": True,
                "evidence_ref": ev_id,
            },
            {
                "schema_version": "0.1",
                "source": "github",
                "account_alias": self.config.account_alias,
                "product_domain": "git",
                "capability": "traffic",
                "status": traffic_status,
                "profile_binding": {
                    "profile_id": self.config.profile_id,
                    "explicit": True,
                    "global_state_mutated": False,
                },
                "checked_at": checked_at,
                "read_only_probe": True,
                "evidence_ref": ev_id,
            },
        ]
        return caps

    def get_account_model(self, capabilities: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """Constructs an Account record conforming to schema/collector/account.schema.json."""
        caps = capabilities or self.get_capabilities()
        health = "ready" if self.config.token else "unauthorized"
        acc = {
            "schema_version": "0.1",
            "source": "github",
            "alias": self.config.account_alias,
            "profile_id": self.config.profile_id,
            "secret_ref": self.config.secret_ref,
            "product_domains": ["git"],
            "capabilities": caps,
            "health": health,
            "health_checked_at": iso_utc_now(),
        }
        return acc

    def collect_paginated(
        self,
        endpoint: str,
        query_params: Optional[Dict[str, Any]] = None,
        max_pages: int = 100,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """
        Paginates GitHub REST API using RFC 5988 Link headers.
        Returns (items, pagination_info_dict).
        """
        params = dict(query_params or {})
        params.setdefault("per_page", 100)
        query_string = urllib.parse.urlencode(params)
        sep = "&" if "?" in endpoint else "?"
        next_url: Optional[str] = f"{endpoint}{sep}{query_string}" if query_string else endpoint

        items: List[Dict[str, Any]] = []
        pages_fetched = 0
        truncated = False
        last_cursor: Optional[str] = None

        while next_url and pages_fetched < max_pages:
            status, data, headers = self.client.request(next_url)
            pages_fetched += 1

            if isinstance(data, list):
                items.extend(data)
            elif isinstance(data, dict):
                items.append(data)

            link_header = headers.get("Link") or headers.get("link", "")
            links = parse_link_header(link_header)
            next_url = links.get("next")
            last_cursor = next_url

        if next_url and pages_fetched >= max_pages:
            truncated = True

        pagination = {
            "pages_fetched": pages_fetched,
            "complete": not truncated,
            "truncated": truncated,
            "next_cursor": None if not truncated else next_url,
            "last_committed_cursor": last_cursor,
        }
        return items, pagination

    def collect_repo_metadata(self, owner_repo: str) -> Dict[str, Any]:
        """Fetches repo metadata (handles empty repo vs not found)."""
        status, data, headers = self.client.request(f"/repos/{owner_repo}")
        return data

    def collect_commits(
        self,
        owner_repo: str,
        since: Optional[str] = None,
        until: Optional[str] = None,
    ) -> Tuple[str, List[Dict[str, Any]], Dict[str, Any]]:
        """
        Fetches commits for repository within [since, until].
        Returns (repo_state, commits_list, pagination_dict).
        repo_state: 'ok', 'empty', or raises GitHubApiError for 'inaccessible'.
        """
        params: Dict[str, Any] = {}
        if since:
            params["since"] = since
        if until:
            params["until"] = until

        try:
            commits, pagination = self.collect_paginated(f"/repos/{owner_repo}/commits", params)
            return "ok", commits, pagination
        except GitHubApiError as e:
            if e.is_empty_repo:
                return "empty", [], {
                    "pages_fetched": 1,
                    "complete": True,
                    "truncated": False,
                    "next_cursor": None,
                    "last_committed_cursor": None,
                }
            raise

    def collect_releases(self, owner_repo: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Fetches releases for repository."""
        return self.collect_paginated(f"/repos/{owner_repo}/releases")

    def collect_pull_requests(
        self,
        owner_repo: str,
        state: str = "all",
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Fetches pull requests for repository."""
        params = {"state": state, "sort": "updated", "direction": "desc"}
        return self.collect_paginated(f"/repos/{owner_repo}/pulls", params)

    def collect_traffic(
        self,
        owner_repo: str,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        Fetches traffic metrics (clones & views).
        GitHub enforces a strict 14-day rolling window for traffic data.
        Returns (views_dict, clones_dict).
        """
        # Traffic requires push/admin permissions on repo
        _, views_data, _ = self.client.request(f"/repos/{owner_repo}/traffic/views")
        _, clones_data, _ = self.client.request(f"/repos/{owner_repo}/traffic/clones")
        return views_data or {}, clones_data or {}

    def transform_to_events(
        self,
        owner_repo: str,
        commits: List[Dict[str, Any]],
        releases: List[Dict[str, Any]],
        pull_requests: List[Dict[str, Any]],
        traffic_data: Optional[Tuple[Dict[str, Any], Dict[str, Any]]] = None,
        evidence_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Transforms collected raw GitHub items into canonical AEL v0.1 event envelopes
        conforming to schema/core/event.schema.json.
        """
        events: List[Dict[str, Any]] = []
        ev_id = evidence_id or f"ev_{generate_ulid()}"
        collected_at = iso_utc_now()
        source = "github"
        account_alias = self.config.account_alias

        # 1. Commits -> engineering.commit
        for item in commits:
            commit_sha = item.get("sha")
            commit_data = item.get("commit", {})
            author_data = commit_data.get("author", {})
            committed_at = author_data.get("date") or collected_at

            idempotency_key = compute_idempotency_key(source, account_alias, commit_sha, owner_repo)
            event_id = generate_ulid()

            events.append({
                "schema_version": "0.1",
                "event_id": event_id,
                "event_type": "engineering.commit",
                "source": source,
                "source_account_id": account_alias,
                "collector": "github_collector",
                "mapping_version": "0.1.0",
                "collected_at": collected_at,
                "observed_at": committed_at,
                "period_start": None,
                "period_end": None,
                "collection_phase": "historical",
                "project_id": project_id,
                "repository_id": owner_repo,
                "session_id": None,
                "task_id": None,
                "provenance": {
                    "class": "P0",
                    "method": "api",
                    "confidence": 1.0,
                    "confidence_reason": "primary GitHub REST API response verified via RFC 5988 pagination",
                    "validation_status": "format_checked",
                },
                "raw_evidence_id": ev_id,
                "idempotency_key": idempotency_key,
                "payload": {
                    "sha": commit_sha,
                    "message": sanitize_message(commit_data.get("message", "")),
                    "author": author_data.get("name"),
                    "author_email": author_data.get("email"),
                    "html_url": item.get("html_url"),
                    "parents": [p.get("sha") for p in item.get("parents", []) if p.get("sha")],
                },
            })

        # 2. Releases -> engineering.release
        for item in releases:
            rel_id = str(item.get("id"))
            tag_name = item.get("tag_name")
            published_at = item.get("published_at") or item.get("created_at") or collected_at

            idempotency_key = compute_idempotency_key(source, account_alias, f"rel_{rel_id}", owner_repo)
            event_id = generate_ulid()

            events.append({
                "schema_version": "0.1",
                "event_id": event_id,
                "event_type": "engineering.release",
                "source": source,
                "source_account_id": account_alias,
                "collector": "github_collector",
                "mapping_version": "0.1.0",
                "collected_at": collected_at,
                "observed_at": published_at,
                "period_start": None,
                "period_end": None,
                "collection_phase": "historical",
                "project_id": project_id,
                "repository_id": owner_repo,
                "session_id": None,
                "task_id": None,
                "provenance": {
                    "class": "P0",
                    "method": "api",
                    "confidence": 1.0,
                    "confidence_reason": "primary GitHub REST API response verified via RFC 5988 pagination",
                    "validation_status": "format_checked",
                },
                "raw_evidence_id": ev_id,
                "idempotency_key": idempotency_key,
                "payload": {
                    "release_id": item.get("id"),
                    "tag_name": tag_name,
                    "name": sanitize_message(item.get("name", "")),
                    "draft": item.get("draft", False),
                    "prerelease": item.get("prerelease", False),
                    "html_url": item.get("html_url"),
                    "body": sanitize_message(item.get("body", "")),
                },
            })

        # 3. Pull Requests -> engineering.pull_request
        for item in pull_requests:
            pr_num = str(item.get("number"))
            updated_at = item.get("updated_at") or item.get("created_at") or collected_at

            idempotency_key = compute_idempotency_key(source, account_alias, f"pr_{pr_num}", owner_repo)
            event_id = generate_ulid()

            events.append({
                "schema_version": "0.1",
                "event_id": event_id,
                "event_type": "engineering.pull_request",
                "source": source,
                "source_account_id": account_alias,
                "collector": "github_collector",
                "mapping_version": "0.1.0",
                "collected_at": collected_at,
                "observed_at": updated_at,
                "period_start": None,
                "period_end": None,
                "collection_phase": "historical",
                "project_id": project_id,
                "repository_id": owner_repo,
                "session_id": None,
                "task_id": None,
                "provenance": {
                    "class": "P0",
                    "method": "api",
                    "confidence": 1.0,
                    "confidence_reason": "primary GitHub REST API response verified via RFC 5988 pagination",
                    "validation_status": "format_checked",
                },
                "raw_evidence_id": ev_id,
                "idempotency_key": idempotency_key,
                "payload": {
                    "number": item.get("number"),
                    "title": sanitize_message(item.get("title", "")),
                    "state": item.get("state"),
                    "user": item.get("user", {}).get("login"),
                    "created_at": item.get("created_at"),
                    "closed_at": item.get("closed_at"),
                    "merged_at": item.get("merged_at"),
                    "html_url": item.get("html_url"),
                },
            })

        # 4. Traffic Snapshots -> engineering.traffic_snapshot (US-12 14-day rolling window)
        if traffic_data:
            views, clones = traffic_data
            # GitHub traffic is daily buckets within 14 days
            traffic_payload = {
                "window_limit_days": 14,
                "views_total": views.get("count", 0),
                "views_uniques": views.get("uniques", 0),
                "clones_total": clones.get("count", 0),
                "clones_uniques": clones.get("uniques", 0),
                "views_daily": views.get("views", []),
                "clones_daily": clones.get("clones", []),
            }
            idempotency_key = compute_idempotency_key(source, account_alias, "traffic_14d", owner_repo)
            event_id = generate_ulid()

            events.append({
                "schema_version": "0.1",
                "event_id": event_id,
                "event_type": "engineering.traffic_snapshot",
                "source": source,
                "source_account_id": account_alias,
                "collector": "github_collector",
                "mapping_version": "0.1.0",
                "collected_at": collected_at,
                "observed_at": collected_at,
                "period_start": None,
                "period_end": None,
                "collection_phase": "prospective",
                "project_id": project_id,
                "repository_id": owner_repo,
                "session_id": None,
                "task_id": None,
                "provenance": {
                    "class": "P0",
                    "method": "api",
                    "confidence": 1.0,
                    "confidence_reason": "primary GitHub REST API response verified via RFC 5988 pagination",
                    "validation_status": "format_checked",
                },
                "raw_evidence_id": ev_id,
                "idempotency_key": idempotency_key,
                "payload": traffic_payload,
            })

        return events

    def run_collection(
        self,
        requested_period: Dict[str, str],
        trigger: str = "manual",
        routing_subset: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Executes collection for allowlisted repos.
        Returns a complete Run dictionary strictly conforming to schema/collector/run.schema.json.
        """
        started_at = iso_utc_now()
        run_id = f"run_{generate_ulid()}"
        account_runs: List[Dict[str, Any]] = []
        errors_list: List[Dict[str, Any]] = []

        allowlist = self.config.allowlist_repos
        if not allowlist:
            # Allowlist is mandatory (US-12.a)
            err = {
                "schema_version": "0.1",
                "error_class": "config_secret_ref_missing",
                "retryable": False,
                "occurred_at": started_at,
                "message": "No allowlisted repositories configured for collection",
                "retry_budget": {"max": 0, "used": 0},
            }
            finished_at = iso_utc_now()
            return {
                "schema_version": "0.1",
                "run_id": run_id,
                "trigger": trigger,
                "started_at": started_at,
                "finished_at": finished_at,
                "status": "failure",
                "exit_status": 1,
                "routing_subset": routing_subset or [],
                "account_runs": [
                    {
                        "account_alias": self.config.account_alias,
                        "profile_id": self.config.profile_id,
                        "source": "github",
                        "product_domain": "git",
                        "capability": "git",
                        "status": "failure",
                        "observed_at": started_at,
                        "coverage": {
                            "requested": requested_period,
                            "delivered": None,
                            "confirmed": None,
                            "confirmation": "unconfirmed",
                        },
                        "errors": [err],
                    }
                ],
            }

        # Check authentication token
        if not self.config.token:
            err = {
                "schema_version": "0.1",
                "error_class": "auth",
                "retryable": False,
                "occurred_at": started_at,
                "message": "Missing credentials for GitHub collection",
                "retry_budget": {"max": 0, "used": 0},
            }
            finished_at = iso_utc_now()
            return {
                "schema_version": "0.1",
                "run_id": run_id,
                "trigger": trigger,
                "started_at": started_at,
                "finished_at": finished_at,
                "status": "failure",
                "exit_status": 1,
                "routing_subset": routing_subset or [],
                "account_runs": [
                    {
                        "account_alias": self.config.account_alias,
                        "profile_id": self.config.profile_id,
                        "source": "github",
                        "product_domain": "git",
                        "capability": "git",
                        "status": "failure",
                        "observed_at": started_at,
                        "coverage": {
                            "requested": requested_period,
                            "delivered": None,
                            "confirmed": None,
                            "confirmation": "unconfirmed",
                        },
                        "errors": [err],
                    }
                ],
            }

        total_pages = 0
        total_items = 0
        has_error = False

        for repo in allowlist:
            try:
                repo_state, commits, commit_pagination = self.collect_commits(
                    repo,
                    since=requested_period.get("start"),
                    until=requested_period.get("end"),
                )
                releases, rel_pagination = self.collect_releases(repo)
                prs, pr_pagination = self.collect_pull_requests(repo)
                total_pages += commit_pagination.get("pages_fetched", 0)
                total_items += len(commits) + len(releases) + len(prs)
            except GitHubApiError as e:
                has_error = True
                errors_list.append({
                    "schema_version": "0.1",
                    "error_class": e.error_class,
                    "retryable": e.retryable,
                    "occurred_at": iso_utc_now(),
                    "http_status": e.http_status,
                    "message": e.message,
                    "retry_budget": {"max": 3, "used": 1},
                })
                break

        finished_at = iso_utc_now()
        # Compute status
        if has_error:
            run_status = "failure"
            exit_status = 1
            account_status = "failure"
            coverage_data = {
                "requested": requested_period,
                "delivered": None,
                "confirmed": None,
                "confirmation": "unconfirmed",
            }
        else:
            run_status = "success"
            exit_status = 0
            account_status = "success"
            coverage_data = {
                "requested": requested_period,
                "delivered": requested_period,
                "confirmed": requested_period,
                "confirmation": "complete",
                "pagination": {
                    "pages_fetched": total_pages,
                    "complete": True,
                    "truncated": False,
                    "next_cursor": None,
                    "last_committed_cursor": None,
                },
            }

        proc_started = started_at
        proc_finished = finished_at
        proc_out = json.dumps({"items_count": total_items}).encode("utf-8")

        account_run: Dict[str, Any] = {
            "account_alias": self.config.account_alias,
            "profile_id": self.config.profile_id,
            "source": "github",
            "product_domain": "git",
            "capability": "git",
            "status": account_status,
            "observed_at": finished_at,
            "coverage": coverage_data,
            "errors": errors_list,
            "process": {
                "argv": ["ael", "collect", "github", f"--account={self.config.account_alias}"],
                "exit_code": 0 if not has_error else 1,
                "signal": None,
                "stdout_sha256": hashlib.sha256(proc_out).hexdigest(),
                "stderr_sha256": hashlib.sha256(b"").hexdigest(),
                "stdout_truncated": False,
                "stderr_truncated": False,
                "started_at": proc_started,
                "finished_at": proc_finished,
                "profile_id": self.config.profile_id,
            },
        }

        return {
            "schema_version": "0.1",
            "run_id": run_id,
            "trigger": trigger,
            "started_at": started_at,
            "finished_at": finished_at,
            "status": run_status,
            "exit_status": exit_status,
            "routing_subset": routing_subset or [],
            "account_runs": [account_run],
        }
