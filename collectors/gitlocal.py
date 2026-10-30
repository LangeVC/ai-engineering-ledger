"""Deterministic engineering-output metrics from allowlisted local Git repositories."""

from __future__ import annotations

import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _default_runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


def classify_path(path: str) -> str:
    normalized = path.replace("\\", "/").lower()
    name = normalized.rsplit("/", 1)[-1]
    suffix = Path(name).suffix
    if normalized.startswith(("vendor/", "node_modules/", "third_party/")):
        return "vendor"
    if normalized.startswith(("dist/", "build/", "generated/")) or name.endswith((".min.js", ".min.css")):
        return "generated"
    if normalized.startswith(("tests/", "test/", "spec/")) or name.startswith("test_") or ".test." in name:
        return "tests"
    if normalized.startswith(("docs/", "documentation/")) or suffix in {".md", ".rst", ".adoc"}:
        return "documentation"
    if normalized.startswith(("config/", ".github/", ".forgejo/")) or suffix in {
        ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    }:
        return "configuration"
    return "code"


class LocalGitCollector:
    """Read committed objects only; never infer releases from tags."""

    def __init__(self, allowlisted_roots: Sequence[str], runner: CommandRunner | None = None):
        self.allowlisted_roots = {str(Path(root).resolve()) for root in allowlisted_roots}
        self.runner = runner or _default_runner

    def _run(self, repository: str, *args: str) -> str:
        result = self.runner(["git", "-C", repository, *args])
        if result.returncode != 0:
            raise RuntimeError((result.stderr or "git command failed").strip())
        return result.stdout

    def scan(
        self,
        repository: str,
        period_start: str,
        period_end_exclusive: str,
        revision: str = "HEAD",
    ) -> dict[str, object]:
        root = str(Path(repository).resolve())
        if root not in self.allowlisted_roots:
            raise PermissionError("repository is not allowlisted")
        start = datetime.fromisoformat(period_start.replace("Z", "+00:00"))
        end = datetime.fromisoformat(period_end_exclusive.replace("Z", "+00:00"))
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("reporting interval timestamps must include a timezone")
        if start >= end:
            raise ValueError("reporting interval must be increasing")

        resolved_revision = self._run(root, "rev-parse", "--verify", revision).strip()
        log = self._run(root, "log", resolved_revision, "--format=%H%x00%aI%x00%cI%x00%P")
        commits: list[tuple[str, str, str, str]] = []
        for line in log.splitlines():
            fields = line.split("\x00")
            if len(fields) != 4:
                continue
            authored = datetime.fromisoformat(fields[1].replace("Z", "+00:00"))
            if start <= authored < end:
                commits.append((fields[0], fields[1], fields[2], fields[3]))

        categories: Counter[str] = Counter()
        additions = deletions = binary_files = file_changes = 0
        active_days: set[str] = set()
        merge_commits = 0
        for sha, authored_at, _committed_at, parents in commits:
            active_days.add(authored_at[:10])
            if len(parents.split()) > 1:
                merge_commits += 1
            numstat = self._run(root, "show", "--format=", "--numstat", "--no-renames", sha)
            for line in numstat.splitlines():
                fields = line.split("\t", 2)
                if len(fields) != 3:
                    continue
                added, deleted, path = fields
                file_changes += 1
                categories[classify_path(path)] += 1
                if added == "-" or deleted == "-":
                    binary_files += 1
                else:
                    additions += int(added)
                    deletions += int(deleted)

        tags = sorted(filter(None, self._run(root, "tag", "--merged", resolved_revision).splitlines()))
        return {
            "schema_version": "0.3.0",
            "repository_id": Path(root).name,
            "revision": resolved_revision,
            "period": {"start": period_start, "end_exclusive": period_end_exclusive},
            "metrics": {
                "commits": len(commits),
                "merge_commits": merge_commits,
                "active_days": len(active_days),
                "file_changes": file_changes,
                "additions": additions,
                "deletions": deletions,
                "binary_files": binary_files,
                "file_categories": {key: categories.get(key, 0) for key in (
                    "code", "tests", "documentation", "configuration", "generated", "vendor"
                )},
            },
            "git_tags": tags,
            "releases": [],
            "manifest": {
                "object_policy": "committed_objects_at_resolved_revision",
                "time_policy": "author_time_half_open_interval",
                "merge_policy": "include_and_report_separately",
                "tag_policy": "tags_are_not_releases_without_forge_evidence",
                "parser_version": "gitlocal-v0.3.0",
            },
        }
