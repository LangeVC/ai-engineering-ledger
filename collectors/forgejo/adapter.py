"""
Forgejo Collector Adapter Implementation.
Satisfies PRD US-12, US-13, and decision D01.
"""

from dataclasses import dataclass, field
import hashlib
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .client import ForgejoApiClient, ForgejoApiError, GitSSHClient
from .pagination import parse_link_header

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


@dataclass
class ForgejoAccountConfig:
    alias: str = "default"
    profile_id: str = "forgejo_default"
    host: str = "forge.example.invalid"
    base_url: str = "https://forge.example.invalid"
    api_token: Optional[str] = None
    secret_ref: Optional[str] = None  # e.g. "env:FORGEJO_TOKEN"


@dataclass
class ForgejoRepoConfig:
    owner: str
    name: str
    local_path: Optional[str] = None
    remote_ssh_url: Optional[str] = None
    role_assumption: str = "unassigned_candidate"  # D01: neither canonical nor mirror by default


@dataclass
class CollectionResult:
    status: str  # "success", "partial_failure", "failure"
    exit_status: int  # 0, 10, 20
    observations: List[Dict[str, Any]] = field(default_factory=list)
    events: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[Dict[str, Any]] = field(default_factory=list)
    capabilities: List[Dict[str, Any]] = field(default_factory=list)
    account_run: Optional[Dict[str, Any]] = None
    run_record: Optional[Dict[str, Any]] = None


def generate_idempotency_key(source: str, account: str, entity_type: str, entity_id: str, scope: str = "") -> str:
    """Computes stable sha256 idempotency key per event schema requirements."""
    content = f"{source}:{account}:{entity_type}:{entity_id}:{scope}"
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def compute_evidence_hash(data_bytes: bytes) -> str:
    return hashlib.sha256(data_bytes).hexdigest()


class ForgejoAdapter:
    """
    Adapter implementing the collection contract for Forgejo.
    Supports both API-driven collection (when token configured)
    and Git-SSH fallback (PRD US-12.b).
    """

    def __init__(
        self,
        account_config: ForgejoAccountConfig,
        repo_configs: List[ForgejoRepoConfig],
        api_client: Optional[ForgejoApiClient] = None,
        git_client: Optional[GitSSHClient] = None,
    ):
        self.account_config = account_config
        self.repo_configs = repo_configs
        self.api_client = api_client or ForgejoApiClient(
            base_url=account_config.base_url,
            token=account_config.api_token
        )
        self.git_client = git_client or GitSSHClient(host=account_config.host)

    def assess_capabilities(self, now_iso: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Assesses capabilities and declares them adhering to capability.schema.json.
        """
        now = now_iso or datetime.now(timezone.utc).isoformat()
        caps = []

        # 1. Git SSH capability (always verifiable via SSH)
        ssh_status = self.git_client.check_ssh_access()
        if ssh_status["authenticated"]:
            caps.append({
                "schema_version": "0.1",
                "source": "forgejo",
                "account_alias": self.account_config.alias,
                "product_domain": "git",
                "capability": "git",
                "status": "ready",
                "profile_binding": {
                    "profile_id": self.account_config.profile_id,
                    "explicit": True,
                    "global_state_mutated": False
                },
                "checked_at": now,
                "read_only_probe": True,
                "evidence_ref": "ev_forgejo_git_ssh_probe"
            })
        else:
            caps.append({
                "schema_version": "0.1",
                "source": "forgejo",
                "account_alias": self.account_config.alias,
                "product_domain": "git",
                "capability": "git",
                "status": "unauthorized",
                "profile_binding": {
                    "profile_id": self.account_config.profile_id,
                    "explicit": True,
                    "global_state_mutated": False
                },
                "checked_at": now,
                "read_only_probe": True,
                "evidence_ref": "ev_forgejo_git_ssh_probe"
            })

        # 2. Historical / API capability
        if self.account_config.api_token:
            caps.append({
                "schema_version": "0.1",
                "source": "forgejo",
                "account_alias": self.account_config.alias,
                "product_domain": "git",
                "capability": "historical",
                "status": "ready",
                "profile_binding": {
                    "profile_id": self.account_config.profile_id,
                    "explicit": True,
                    "global_state_mutated": False
                },
                "checked_at": now,
                "read_only_probe": True,
                "evidence_ref": "ev_forgejo_api_token_probe"
            })
        else:
            # Unverified per US-12.b and capabilities report
            caps.append({
                "schema_version": "0.1",
                "source": "forgejo",
                "account_alias": self.account_config.alias,
                "product_domain": "git",
                "capability": "historical",
                "status": "unverified",
                "profile_binding": {
                    "profile_id": self.account_config.profile_id,
                    "explicit": True,
                    "global_state_mutated": False
                },
                "checked_at": now,
                "reason": "Forgejo API requires personal access token. No token configured in environment or account profile."
            })

        return caps

    def collect(
        self,
        requested_period: Optional[Dict[str, str]] = None,
        run_id: str = "run_forgejo_01",
        trigger: str = "manual"
    ) -> CollectionResult:
        """
        Executes collection across configured repositories.
        """
        started_at = datetime.now(timezone.utc).isoformat()
        period = requested_period or {
            "start": "2026-03-01T00:00:00Z",
            "end": datetime.now(timezone.utc).isoformat()
        }

        observations: List[Dict[str, Any]] = []
        events: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []
        total_pages = 0
        repo_runs = []

        capabilities = self.assess_capabilities(started_at)
        has_api_access = any(c["capability"] == "historical" and c["status"] == "ready" for c in capabilities)

        for repo in self.repo_configs:
            repo_id = f"{repo.owner}/{repo.name}"
            remote_ssh = repo.remote_ssh_url or f"git@{self.account_config.host}:{repo.owner}/{repo.name}.git"

            # Check repo status via git protocol
            git_check = self.git_client.inspect_remote_refs(remote_ssh)

            if git_check["is_not_found"]:
                # US-12.c: Distinct state "unauthorized / inaccessible"
                observations.append({
                    "state": "unauthorized",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "account_alias": self.account_config.alias,
                    "product_domain": "git",
                    "reason": f"Repository {repo_id} inaccessible or does not exist (git ls-remote code 128)"
                })
                errors.append({
                    "schema_version": "0.1",
                    "error_class": "unauthorized",
                    "retryable": False,
                    "occurred_at": datetime.now(timezone.utc).isoformat(),
                    "retry_budget": {"max": 0, "used": 0},
                    "message": f"Repository {repo_id} not found or access denied"
                })
                continue

            if git_check["is_empty"]:
                # US-12.c: Distinct state "empty"
                observations.append({
                    "state": "evidenced_zero",
                    "value": 0,
                    "unit": "commits",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "account_alias": self.account_config.alias,
                    "product_domain": "git",
                    "evidence_ref": "ev_forgejo_lsremote_empty",
                    "reason": f"Repository {repo_id} exists but is empty (zero refs)"
                })

            if has_api_access:
                # Collect via API
                try:
                    # 1. Commits with pagination
                    page = 1
                    while True:
                        commits_data, headers = self.api_client.list_commits(repo.owner, repo.name, page=page)
                        total_pages += 1
                        if not commits_data:
                            break
                        for c in commits_data:
                            sha = c.get("sha")
                            commit_info = c.get("commit", {})
                            events.append({
                                "schema_version": "0.1",
                                "event_id": generate_ulid(),
                                "event_type": "engineering.commit",
                                "source": "forgejo",
                                "source_account_id": self.account_config.alias,
                                "collector": "collectors.forgejo",
                                "mapping_version": "0.1.0",
                                "collected_at": datetime.now(timezone.utc).isoformat(),
                                "observed_at": commit_info.get("committer", {}).get("date") or commit_info.get("author", {}).get("date"),
                                "period_start": None,
                                "period_end": None,
                                "collection_phase": "historical",
                                "project_id": "ai-engineering-ledger",
                                "repository_id": repo_id,
                                "session_id": None,
                                "task_id": None,
                                "provenance": {
                                    "class": "P0",
                                    "method": "api",
                                    "confidence": 1.0,
                                    "confidence_reason": "Forgejo REST API commit endpoint",
                                    "validation_status": "format_checked"
                                },
                                "raw_evidence_id": f"ev_{generate_ulid()}",
                                "idempotency_key": generate_idempotency_key("forgejo", self.account_config.alias, "commit", sha),
                                "payload": {
                                    "sha": sha,
                                    "message": commit_info.get("message"),
                                    "author": commit_info.get("author"),
                                    "committer": commit_info.get("committer")
                                }
                            })
                        links = parse_link_header(headers.get("Link") or headers.get("link"))
                        if "next" in links:
                            page += 1
                        else:
                            break

                    # 2. Releases
                    releases_data, _ = self.api_client.list_releases(repo.owner, repo.name)
                    for rel in releases_data:
                        rel_id = str(rel.get("id"))
                        events.append({
                            "schema_version": "0.1",
                            "event_id": generate_ulid(),
                            "event_type": "engineering.release",
                            "source": "forgejo",
                            "source_account_id": self.account_config.alias,
                            "collector": "collectors.forgejo",
                            "mapping_version": "0.1.0",
                            "collected_at": datetime.now(timezone.utc).isoformat(),
                            "observed_at": rel.get("published_at") or rel.get("created_at"),
                            "period_start": None,
                            "period_end": None,
                            "collection_phase": "historical",
                            "project_id": "ai-engineering-ledger",
                            "repository_id": repo_id,
                            "session_id": None,
                            "task_id": None,
                            "provenance": {
                                "class": "P0",
                                "method": "api",
                                "confidence": 1.0,
                                "confidence_reason": "Forgejo REST API releases endpoint",
                                "validation_status": "format_checked"
                            },
                            "raw_evidence_id": f"ev_{generate_ulid()}",
                            "idempotency_key": generate_idempotency_key("forgejo", self.account_config.alias, "release", rel_id),
                            "payload": rel
                        })

                    # 3. Pull requests
                    prs_data, _ = self.api_client.list_pull_requests(repo.owner, repo.name)
                    for pr in prs_data:
                        pr_num = str(pr.get("number"))
                        events.append({
                            "schema_version": "0.1",
                            "event_id": generate_ulid(),
                            "event_type": "engineering.pull_request",
                            "source": "forgejo",
                            "source_account_id": self.account_config.alias,
                            "collector": "collectors.forgejo",
                            "mapping_version": "0.1.0",
                            "collected_at": datetime.now(timezone.utc).isoformat(),
                            "observed_at": pr.get("updated_at") or pr.get("created_at"),
                            "period_start": None,
                            "period_end": None,
                            "collection_phase": "historical",
                            "project_id": "ai-engineering-ledger",
                            "repository_id": repo_id,
                            "session_id": None,
                            "task_id": None,
                            "provenance": {
                                "class": "P0",
                                "method": "api",
                                "confidence": 1.0,
                                "confidence_reason": "Forgejo REST API pull request endpoint",
                                "validation_status": "format_checked"
                            },
                            "raw_evidence_id": f"ev_{generate_ulid()}",
                            "idempotency_key": generate_idempotency_key("forgejo", self.account_config.alias, "pr", pr_num),
                            "payload": pr
                        })

                except ForgejoApiError as e:
                    errors.append({
                        "schema_version": "0.1",
                        "error_class": "unauthorized" if e.status_code == 403 else "protocol",
                        "retryable": False,
                        "occurred_at": datetime.now(timezone.utc).isoformat(),
                        "http_status": e.status_code,
                        "retry_budget": {"max": 0, "used": 0},
                        "message": f"Forgejo API call failed: {str(e)}"
                    })
            else:
                # US-12.b: API unverified / inaccessible -> fallback to local Git if present
                observations.append({
                    "state": "unverified",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "account_alias": self.account_config.alias,
                    "product_domain": "git",
                    "reason": "Forgejo PR/Release API data unavailable (unverified credentials). Local Git fallback active."
                })

                if repo.local_path and os.path.exists(repo.local_path):
                    local_git = self.git_client.inspect_local_git_repo(repo.local_path)
                    for c in local_git["commits"]:
                        sha = c["sha"]
                        events.append({
                            "schema_version": "0.1",
                            "event_id": generate_ulid(),
                            "event_type": "engineering.commit",
                            "source": "forgejo",
                            "source_account_id": self.account_config.alias,
                            "collector": "collectors.forgejo.git_fallback",
                            "mapping_version": "0.1.0",
                            "collected_at": datetime.now(timezone.utc).isoformat(),
                            "observed_at": c["committer"]["date"],
                            "period_start": None,
                            "period_end": None,
                            "collection_phase": "historical",
                            "project_id": "ai-engineering-ledger",
                            "repository_id": repo_id,
                            "session_id": None,
                            "task_id": None,
                            "provenance": {
                                "class": "P1",
                                "method": "local",
                                "confidence": 0.95,
                                "confidence_reason": "Local git repository fallback inspection",
                                "validation_status": "format_checked"
                            },
                            "raw_evidence_id": f"ev_{generate_ulid()}",
                            "idempotency_key": generate_idempotency_key("forgejo", self.account_config.alias, "commit", sha),
                            "payload": c
                        })

                    # Note regarding tags: local tags are recorded as observations or git tags, NEVER as forge releases (US-12.b)
                    if local_git["tags"]:
                        observations.append({
                            "state": "value",
                            "value": len(local_git["tags"]),
                            "unit": "git_tags",
                            "observed_at": datetime.now(timezone.utc).isoformat(),
                            "account_alias": self.account_config.alias,
                            "product_domain": "git",
                            "evidence_ref": "ev_forgejo_local_git_tags",
                            "reason": "Local git tags captured; NOT emitted as Forge releases per US-12.b"
                        })

        finished_at = datetime.now(timezone.utc).isoformat()

        # Determine overall run status conforming to ADR-0002 & run.schema.json
        if not errors:
            status = "success"
            exit_status = 0
            acc_status = "success"
        else:
            status = "failure"
            exit_status = 20
            acc_status = "failure"

        # Conforming account_run & run schema
        pagination_info = {
            "pages_fetched": total_pages,
            "complete": True,
            "truncated": False,
            "next_cursor": None,
            "last_committed_cursor": None
        }

        account_run = {
            "account_alias": self.account_config.alias,
            "profile_id": self.account_config.profile_id,
            "source": "forgejo",
            "product_domain": "git",
            "capability": "git",
            "status": acc_status,
            "observed_at": finished_at,
            "coverage": {
                "requested": period,
                "delivered": period if status == "success" else None,
                "confirmed": period if status == "success" else None,
                "confirmation": "complete" if status == "success" else "unconfirmed",
                "pagination": pagination_info
            },
            "errors": errors,
            "process": {
                "argv": ["python3", "-m", "collectors.forgejo"],
                "exit_code": 0 if status == "success" else 1,
                "signal": None,
                "stdout_sha256": hashlib.sha256(b"").hexdigest(),
                "stderr_sha256": hashlib.sha256(b"").hexdigest(),
                "stdout_truncated": False,
                "stderr_truncated": False,
                "started_at": started_at,
                "finished_at": finished_at,
                "profile_id": self.account_config.profile_id
            }
        }

        run_record = {
            "schema_version": "0.1",
            "run_id": run_id,
            "trigger": trigger,
            "started_at": started_at,
            "finished_at": finished_at,
            "status": status,
            "exit_status": exit_status,
            "routing_subset": [repo.name for repo in self.repo_configs],
            "account_runs": [account_run]
        }

        return CollectionResult(
            status=status,
            exit_status=exit_status,
            observations=observations,
            events=events,
            errors=errors,
            capabilities=capabilities,
            account_run=account_run,
            run_record=run_record
        )
