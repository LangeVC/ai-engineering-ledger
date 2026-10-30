"""Comparable, privacy-safe repository inventory for GitHub and Forgejo."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from collectors.forge.sync import RepositorySnapshot


@dataclass(frozen=True)
class ScopedRepository:
    """A repository alias approved by the versioned study scope."""

    source: str
    repository_id: str

    def __post_init__(self) -> None:
        if self.source not in {"github", "forgejo"}:
            raise ValueError("source must be github or forgejo")
        if not self.repository_id or "://" in self.repository_id or "@" in self.repository_id:
            raise ValueError("repository_id must be a privacy-safe alias, not a remote URL")


SnapshotFetcher = Callable[[ScopedRepository], RepositorySnapshot]


def _coverage(snapshot: RepositorySnapshot, start: str, end_exclusive: str) -> dict[str, object]:
    complete = snapshot.state in {"ok", "empty"} and snapshot.pagination_complete
    return {
        "requested": {"start": start, "end_exclusive": end_exclusive},
        "status": "complete" if complete else "partial",
        "pagination_complete": snapshot.pagination_complete,
        "source_state": snapshot.state,
    }


def normalize_snapshot(
    snapshot: RepositorySnapshot,
    period_start: str,
    period_end_exclusive: str,
) -> dict[str, object]:
    """Normalize one adapter snapshot without exporting URLs or credentials."""

    if period_start >= period_end_exclusive:
        raise ValueError("reporting interval must be increasing")
    if snapshot.source not in {"github", "forgejo"}:
        raise ValueError("unsupported forge source")

    releases = snapshot.releases if snapshot.api_available else ()
    gaps: list[dict[str, str]] = []
    if not snapshot.api_available:
        gaps.append({"capability": "forge_api", "reason": "unavailable"})
    if not snapshot.pagination_complete:
        gaps.append({"capability": "pagination", "reason": "incomplete"})
    if snapshot.source == "github" and not snapshot.api_available:
        gaps.append({"capability": "traffic", "reason": "unavailable"})

    return {
        "schema_version": "0.3.0",
        "source": snapshot.source,
        "repository_id": snapshot.repository_id,
        "state": snapshot.state,
        "coverage": _coverage(snapshot, period_start, period_end_exclusive),
        "counts": {
            "commits": len(snapshot.commits),
            "releases": len(releases),
            "pull_requests": len(snapshot.pull_requests) if snapshot.api_available else 0,
            "git_tags": len(snapshot.local_tags),
        },
        "classification": {
            "git_tags_are_releases": False,
            "release_source": "forge_api_only",
        },
        "gaps": gaps,
    }


def build_inventory(
    repositories: Iterable[ScopedRepository],
    fetcher: SnapshotFetcher,
    period_start: str,
    period_end_exclusive: str,
) -> dict[str, object]:
    """Collect a deterministic inventory using an injectable read-only fetcher."""

    records = [
        normalize_snapshot(fetcher(repository), period_start, period_end_exclusive)
        for repository in repositories
    ]
    records.sort(key=lambda item: (str(item["source"]), str(item["repository_id"])))
    return {
        "schema_version": "0.3.0",
        "period": {"start": period_start, "end_exclusive": period_end_exclusive},
        "repositories": records,
        "coverage_complete": all(record["coverage"]["status"] == "complete" for record in records),
    }
