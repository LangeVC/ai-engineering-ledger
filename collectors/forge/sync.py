from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass(frozen=True)
class RepositorySnapshot:
    source: str
    repository_id: str
    state: str
    commits: tuple[dict[str, Any], ...] = ()
    releases: tuple[dict[str, Any], ...] = ()
    pull_requests: tuple[dict[str, Any], ...] = ()
    api_available: bool = True
    pagination_complete: bool = True
    local_tags: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if self.state not in {"ok", "empty", "inaccessible"}:
            raise ValueError("invalid repository state")


@dataclass
class ForgeSyncState:
    allowlist: set[tuple[str, str]]
    commits: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)
    releases: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)
    pull_requests: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)

    def sync(self, snapshot: RepositorySnapshot, period_start: str, period_end: str) -> dict[str, Any]:
        scope = (snapshot.source, snapshot.repository_id)
        if scope not in self.allowlist:
            raise PermissionError("repository is not allowlisted")
        if period_start >= period_end:
            raise ValueError("explicit period must be increasing")
        if snapshot.state != "ok":
            return {"repository_id": snapshot.repository_id, "source": snapshot.source,
                    "state": snapshot.state, "api_data": "unavailable" if not snapshot.api_available else "available",
                    "pagination_complete": snapshot.pagination_complete, "counts": {"commits": 0, "releases": 0, "pull_requests": 0}}
        if not snapshot.pagination_complete:
            raise ValueError("incomplete pagination cannot be reported as a complete sync")
        seen = set()
        for commit in snapshot.commits:
            observed = commit.get("committer_at") or commit.get("observed_at")
            if observed and period_start <= observed < period_end:
                key = (*scope, str(commit["sha"]))
                seen.add(key)
                self.commits[key] = {**self.commits.get(key, {}), **commit, "reachable": True}
        checked_repo_keys = [k for k in self.commits if k[:2] == scope]
        for key in checked_repo_keys:
            if key not in seen:
                self.commits[key] = {**self.commits[key], "reachable": False}
        if snapshot.api_available:
            for release in snapshot.releases:
                key = (*scope, str(release["id"]))
                self.releases[key] = {**self.releases.get(key, {}), **release, "active": True}
            for pr in snapshot.pull_requests:
                key = (*scope, str(pr["number"]))
                self.pull_requests[key] = {**self.pull_requests.get(key, {}), **pr, "active": True}
        return {
            "repository_id": snapshot.repository_id, "source": snapshot.source, "state": snapshot.state,
            "api_data": "available" if snapshot.api_available else "unavailable",
            "local_tags_classified_as_releases": False,
            "pagination_complete": True,
            "counts": {"commits": len(snapshot.commits), "releases": len(snapshot.releases) if snapshot.api_available else 0,
                       "pull_requests": len(snapshot.pull_requests) if snapshot.api_available else 0},
        }
