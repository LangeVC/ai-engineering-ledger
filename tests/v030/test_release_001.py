from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import ael
import pytest

from ael.resources import RESOURCE_NAMES, load_json, read_text


ROOT = Path(__file__).resolve().parents[2]
VERSION = "0.4.0"
PACKAGED_RESOURCE_PATHS = {
    "ael/resources/study-v0.3.schema.json",
    "ael/resources/study_scope.json",
    "ael/resources/study_repository_map.json",
}


def test_release_version_is_consistent_and_cli_reports_it() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked_project = next(
        package
        for package in lock["package"]
        if package["name"] == "ai-engineering-ledger"
    )

    assert project["project"]["version"] == VERSION
    assert locked_project["version"] == VERSION
    assert ael.__version__ == VERSION
    result = subprocess.run(
        [sys.executable, "-m", "ael", "--version"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == f"ael {VERSION}"


def test_changelog_describes_release_scope_and_known_limits() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    release = changelog.split("## 0.3.0", 1)[1].split("## 0.2.0", 1)[0].lower()

    for term in (
        "claude",
        "codex",
        "byteplus",
        "study json",
        "dashboard",
        "privacy",
        "known limitations",
        "sqlite",
        "out of scope",
    ):
        assert term in release


def test_packaged_resources_match_release_contract_sources() -> None:
    source_paths = {
        "study-v0.3.schema.json": ROOT / "schema" / "study-v0.3.schema.json",
        "study_scope.json": ROOT / "config" / "study_scope.json",
        "study_repository_map.json": ROOT / "config" / "study_repository_map.json",
    }

    assert RESOURCE_NAMES == frozenset(source_paths)
    for name, source in source_paths.items():
        assert read_text(name) == source.read_text(encoding="utf-8")
    assert load_json("study_scope.json")["status"] == "frozen"
    assert load_json("study-v0.3.schema.json")["$defs"]["claim"]


def test_cli_study_export_smoke_is_data_only_and_hashes_outputs(tmp_path: Path) -> None:
    input_path = tmp_path / "input.json"
    output = tmp_path / "study-export"
    input_path.write_text(
        json.dumps(
            {
                "tables": {},
                "data_state": {
                    "id": "release-smoke-state",
                    "as_of": "2026-11-01T00:00:00Z",
                },
                "source_status": [],
                "missing_periods": [],
                "evidence_hashes": [],
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ael",
            "export",
            "--input",
            str(input_path),
            "--output",
            str(output),
            "--from",
            "2026-04-26",
            "--to",
            "2026-11-01",
            "--timezone",
            "Europe/Berlin",
            "--study-cutoff",
            "2026-11-01T00:00:00Z",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)

    assert payload["status"] == "success"
    assert len(payload["content_hash"]) == 64
    assert (output / "analysis.json").is_file()
    assert (output / "analysis-manifest.json").is_file()
    assert not any(
        (output / name).exists()
        for name in ("article.md", "index.html", "social.json", "seo.json")
    )


def test_built_artifacts_include_resources_and_install_outside_checkout(
    tmp_path: Path,
) -> None:
    wheel_value = os.environ.get("AEL_RELEASE_WHEEL")
    sdist_value = os.environ.get("AEL_RELEASE_SDIST")
    if not wheel_value or not sdist_value:
        pytest.skip("set AEL_RELEASE_WHEEL and AEL_RELEASE_SDIST for release-artifact gate")
    wheel = Path(wheel_value).resolve()
    sdist = Path(sdist_value).resolve()

    with zipfile.ZipFile(wheel) as archive:
        assert PACKAGED_RESOURCE_PATHS <= set(archive.namelist())
    with tarfile.open(sdist, mode="r:gz") as archive:
        names = archive.getnames()
        for resource in PACKAGED_RESOURCE_PATHS:
            assert any(name.endswith("/" + resource) for name in names)

    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    python = venv / "bin" / "python"
    executable = venv / "bin" / "ael"
    clean_env = dict(os.environ)
    clean_env.pop("PYTHONPATH", None)
    subprocess.run(
        [str(python), "-m", "pip", "install", "--no-deps", str(wheel)],
        cwd=tmp_path,
        env=clean_env,
        check=True,
        capture_output=True,
        text=True,
    )
    resource_probe = subprocess.run(
        [
            str(python),
            "-c",
            (
                "import json; from ael.resources import load_json; "
                "print(json.dumps({'scope': load_json('study_scope.json')['status'], "
                "'schema': load_json('study-v0.3.schema.json')['title']}))"
            ),
        ],
        cwd=tmp_path,
        env=clean_env,
        check=True,
        capture_output=True,
        text=True,
    )
    resource_payload = json.loads(resource_probe.stdout)
    assert resource_payload["scope"] == "frozen"
    assert "v0.3" in resource_payload["schema"]
    version = subprocess.run(
        [str(executable), "--version"],
        cwd=tmp_path,
        env=clean_env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert version.stdout.strip() == f"ael {VERSION}"

    output = tmp_path / "installed-study-export"
    export = subprocess.run(
        [
            str(executable),
            "export",
            "--output",
            str(output),
            "--from",
            "2026-04-26",
            "--to",
            "2026-11-01",
            "--timezone",
            "Europe/Berlin",
            "--study-cutoff",
            "2026-11-01T00:00:00Z",
        ],
        cwd=tmp_path,
        env=clean_env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(export.stdout)["status"] == "success"
    assert (output / "analysis.json").is_file()
