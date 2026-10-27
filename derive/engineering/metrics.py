from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class RepoEquivalence:
    logical_repo_id: str
    repositories: frozenset[str]
    confirmed: bool = True


def alias_identity(value: str, salt: bytes) -> str:
    if not salt:
        raise ValueError("non-empty local salt required")
    return "actor_" + hmac.new(salt, value.strip().lower().encode(), hashlib.sha256).hexdigest()[:20]


def _logical_repo(repo: str, mappings: Iterable[RepoEquivalence]) -> str:
    for mapping in mappings:
        if mapping.confirmed and repo in mapping.repositories:
            return mapping.logical_repo_id
    return repo


def deduplicate_commits(commits: Iterable[dict[str, Any]], mappings: Iterable[RepoEquivalence]) -> dict[str, Any]:
    """Deduplicate only inside explicitly equivalent repository histories."""
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    observations = []
    for commit in commits:
        safe = {k: v for k, v in commit.items() if k not in {"message", "diff", "files", "content"}}
        repo, sha = str(safe["repository_id"]), str(safe["sha"])
        logical = _logical_repo(repo, mappings)
        key = (logical, sha)
        observations.append(safe)
        group = groups.setdefault(key, {
            "logical_repo_id": logical, "sha": sha, "sources": [], "repository_ids": [],
            "committer_at": safe["committer_at"], "author_at": safe.get("author_at"),
        })
        group["sources"].append(safe.get("source"))
        group["repository_ids"].append(repo)
    return {
        "count_mode": "logical_repo_commit_hash",
        "commits": list(groups.values()),
        "source_observations": observations,
    }


def active_engineering_days(commits: Iterable[dict[str, Any]], timezone: str) -> dict[str, Any]:
    zone = ZoneInfo(timezone)
    days = {
        datetime.fromisoformat(str(c["committer_at"]).replace("Z", "+00:00")).astimezone(zone).date().isoformat()
        for c in commits
    }
    return {
        "definition": "at_least_one_counted_commit_by_committer_time",
        "timezone": timezone,
        "days": sorted(days),
        "count": len(days),
        "release_activity_separate": True,
        "pull_request_activity_separate": True,
    }
