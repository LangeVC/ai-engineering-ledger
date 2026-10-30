from __future__ import annotations

import os
import subprocess
from pathlib import Path

from collectors.gitlocal import LocalGitCollector, classify_path


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> None:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_classification_keeps_output_categories_distinct() -> None:
    assert classify_path("src/main.py") == "code"
    assert classify_path("tests/test_main.py") == "tests"
    assert classify_path("docs/usage.md") == "documentation"
    assert classify_path("config/app.yaml") == "configuration"
    assert classify_path("build/bundle.js") == "generated"
    assert classify_path("vendor/lib.c") == "vendor"


def test_scan_is_deterministic_and_never_turns_tags_into_releases(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "AEL Test")
    _git(repo, "config", "user.email", "ael@example.invalid")
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "src" / "main.py").write_text("print('ael')\n")
    (repo / "tests" / "test_main.py").write_text("def test_ok(): assert True\n")
    _git(repo, "add", ".")
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": "2026-10-01T10:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-10-01T10:00:00+00:00",
    }
    _git(repo, "commit", "-q", "-m", "initial", env=env)
    _git(repo, "tag", "v0.1.0")

    collector = LocalGitCollector([str(repo)])
    first = collector.scan(repo, "2026-10-01T00:00:00Z", "2026-11-01T00:00:00Z")
    second = collector.scan(repo, "2026-10-01T00:00:00Z", "2026-11-01T00:00:00Z")

    assert first == second
    assert first["metrics"]["commits"] == 1
    assert first["metrics"]["file_categories"]["code"] == 1
    assert first["metrics"]["file_categories"]["tests"] == 1
    assert first["git_tags"] == ["v0.1.0"]
    assert first["releases"] == []
    assert first["manifest"]["time_policy"] == "author_time_half_open_interval"


def test_scan_refuses_non_allowlisted_repository(tmp_path: Path) -> None:
    collector = LocalGitCollector([str(tmp_path / "allowed")])
    try:
        collector.scan(tmp_path / "other", "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z")
    except PermissionError:
        pass
    else:
        raise AssertionError("non-allowlisted repository was accepted")
