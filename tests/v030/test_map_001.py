from __future__ import annotations

import json
from pathlib import Path


def test_repository_map_has_explicit_canonical_and_evidenced_mirror() -> None:
    registry = json.loads(Path("config/study_repository_map.json").read_text())
    assert registry["registry_version"] == 1
    assert registry["policies"]["name_only_mirror_matching"] == "forbidden"
    assert registry["policies"]["remote_urls"] == "forbidden"

    for repository in registry["repositories"]:
        assert repository["canonical_id"]
        assert repository["study_inclusion"] in {"included", "excluded"}
        canonical = [binding for binding in repository["source_bindings"] if binding["role"] == "canonical"]
        assert len(canonical) == 1
        for binding in repository["source_bindings"]:
            if binding["role"] == "mirror":
                assert binding["equivalence_evidence"]["method"] != "name_match"
                assert binding["equivalence_evidence"]["evidence_ref"]

    serialized = json.dumps(registry).lower()
    assert "://" not in serialized
    assert "password" not in serialized
    assert "secret" not in serialized


def test_unassigned_policy_excludes_product_claims() -> None:
    registry = json.loads(Path("config/study_repository_map.json").read_text())
    assert registry["policies"]["unassigned_product_claims"] == "excluded"
    assert isinstance(registry["unassigned_repositories"], list)
