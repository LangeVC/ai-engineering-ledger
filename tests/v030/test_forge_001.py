from __future__ import annotations

import json

from collectors.forge.inventory import ScopedRepository, build_inventory
from collectors.forge.sync import RepositorySnapshot


def test_inventory_is_comparable_private_and_honest_about_coverage() -> None:
    snapshots = {
        ("github", "langevc/example"): RepositorySnapshot(
            source="github",
            repository_id="langevc/example",
            state="ok",
            commits=({"sha": "a"},),
            releases=({"id": 1},),
            pull_requests=({"number": 2},),
            pagination_complete=False,
        ),
        ("forgejo", "langevc/example"): RepositorySnapshot(
            source="forgejo",
            repository_id="langevc/example",
            state="ok",
            commits=({"sha": "b"},),
            releases=({"id": 3},),
            local_tags=({"name": "v0.3.0"},),
        ),
    }

    inventory = build_inventory(
        [ScopedRepository(*key) for key in reversed(tuple(snapshots))],
        lambda repository: snapshots[(repository.source, repository.repository_id)],
        "2026-01-01T00:00:00Z",
        "2026-11-01T00:00:00Z",
    )

    assert inventory["coverage_complete"] is False
    assert [item["source"] for item in inventory["repositories"]] == ["forgejo", "github"]
    forgejo = next(item for item in inventory["repositories"] if item["source"] == "forgejo")
    github = next(item for item in inventory["repositories"] if item["source"] == "github")
    assert github["coverage"]["status"] == "partial"
    assert {gap["capability"] for gap in github["gaps"]} == {"pagination"}
    assert forgejo["counts"]["releases"] == 1
    assert forgejo["counts"]["git_tags"] == 1
    assert forgejo["classification"]["git_tags_are_releases"] is False
    serialized = json.dumps(inventory).lower()
    assert "https://" not in serialized
    assert "token" not in serialized


def test_unavailable_api_records_traffic_and_api_gaps() -> None:
    snapshot = RepositorySnapshot(
        source="github",
        repository_id="langevc/private",
        state="inaccessible",
        api_available=False,
        pagination_complete=False,
    )
    inventory = build_inventory(
        [ScopedRepository("github", "langevc/private")],
        lambda _repository: snapshot,
        "2026-01-01T00:00:00Z",
        "2026-02-01T00:00:00Z",
    )
    gaps = {gap["capability"] for gap in inventory["repositories"][0]["gaps"]}
    assert gaps == {"forge_api", "pagination", "traffic"}
