import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCOPE_PATH = ROOT / "config" / "study_scope.json"


def load_scope():
    return json.loads(SCOPE_PATH.read_text(encoding="utf-8"))


def test_scope_is_versioned_and_uses_half_open_interval():
    scope = load_scope()

    assert scope["schema_version"] == "0.3.0"
    assert scope["status"] == "frozen"
    assert scope["scope_id"]
    assert scope["scope_version"] >= 1
    assert scope["timezone"] == "Europe/Berlin"
    assert scope["reporting_interval"]["semantics"] == "half-open"
    assert scope["reporting_interval"]["notation"] == "[start, end_exclusive)"
    assert scope["reporting_interval"]["start"] == "2026-04-26"
    assert scope["reporting_interval"]["end_exclusive"] == "2026-11-01"
    assert scope["reporting_interval"]["selection_status"] == "frozen"
    assert "Europe/Berlin local midnight" in scope["reporting_interval"]["interpretation"]


def test_scope_records_cutover_cutoff_and_non_overwrite_policy():
    scope = load_scope()

    assert scope["analysis_cutoff"] == {
        "at": "2026-11-01T00:00:00Z",
        "status": "frozen",
        "semantics": "Only evidence available at or before this cutoff may contribute to the frozen v0.3 study build.",
    }
    assert scope["measurement_cutover"]["retrospective_until_exclusive"] == (
        "2026-10-29T00:00:00+01:00"
    )
    assert scope["measurement_cutover"]["prospective_from"] == "2026-10-29T00:00:00+01:00"
    assert scope["measurement_cutover"]["status"] == "frozen"
    assert "does not imply provider completeness" in scope["measurement_cutover"]["semantics"]
    assert scope["revision_policy"]["mutation"] == "new_scope_version_required"
    assert scope["revision_policy"]["frozen_status"] == "frozen"


def test_source_and_account_aliases_are_stable_unique_and_secret_free():
    scope = load_scope()
    source_aliases = [item["alias"] for item in scope["source_aliases"]]
    account_aliases = [item["alias"] for item in scope["accounts"]]

    assert len(source_aliases) == len(set(source_aliases))
    assert len(account_aliases) == len(set(account_aliases))
    assert {f"byteplus-0{index}" for index in range(1, 6)} <= set(source_aliases)
    assert {"claude-pro", "codex-plus", "antigravity-pro"} <= set(source_aliases)

    serialized = json.dumps(scope).lower()
    for forbidden in ("api_key", "password", "secret_value", "access_token"):
        assert forbidden not in serialized


def test_products_and_repositories_require_alias_or_explicit_unassigned_state():
    scope = load_scope()

    for collection in (scope["products"], scope["repositories"]):
        for item in collection:
            assert item.get("alias") or item.get("assignment_status") == "unassigned"

    assert scope["assignment_policy"]["product_or_repository_without_alias"] == "explicit_unassigned"
    assert scope["assignment_policy"]["unassigned_status"] == "unassigned"
