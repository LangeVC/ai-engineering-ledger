from __future__ import annotations

import json

import pytest

from reconcile.correlation import AssociationLedger, AssociationLink


def link(**overrides: object) -> AssociationLink:
    values: dict[str, object] = {
        "subject_kind": "session",
        "subject_id": "session-01",
        "object_kind": "task",
        "object_id": "TASK-040",
        "association_method": "explicit_id",
        "strength": "strong",
        "evidence_ids": ("sha256:b", "sha256:a"),
    }
    values.update(overrides)
    return AssociationLink(**values)  # type: ignore[arg-type]


def test_explicit_link_has_stable_id_and_canonical_evidence() -> None:
    first = link()
    second = link(evidence_ids=("sha256:a", "sha256:b", "sha256:a"))
    assert first.link_id == second.link_id
    assert first.evidence_ids == ("sha256:a", "sha256:b")
    record = first.as_record()
    assert record["association_method"] == "explicit_id"
    assert record["strength"] == "strong"
    assert record["evidence_ids"] == ["sha256:a", "sha256:b"]
    assert record["confirmed"] is True
    assert record["interpretation"] == "association_only"
    assert "caus" not in json.dumps(record).lower()


@pytest.mark.parametrize("kind", ["task", "session", "repository", "change", "release"])
def test_all_contracted_identity_kinds_can_participate(kind: str) -> None:
    candidate = link(subject_kind=kind, subject_id=f"{kind}-1")
    assert candidate.as_record()["subject"] == {"kind": kind, "id": f"{kind}-1"}


def test_temporal_only_link_is_candidate_and_never_confirmed() -> None:
    temporal = link(
        association_method="temporal_candidate",
        strength="weak",
        evidence_ids=("window:2026-10-30T12",),
    )
    ledger = AssociationLedger([temporal])
    assert temporal.confirmed is False
    assert ledger.partition()["confirmed"] == []
    assert ledger.partition()["candidates"] == [temporal.as_record()]
    assert temporal.as_record()["interpretation"] == "association_only"


@pytest.mark.parametrize("strength", ["strong", "medium"])
def test_temporal_candidate_rejects_attribution_strength(strength: str) -> None:
    with pytest.raises(ValueError, match="temporal candidate"):
        link(
            association_method="temporal_candidate",
            strength=strength,
            evidence_ids=("window:1",),
        )


def test_authoritative_link_requires_evidence() -> None:
    with pytest.raises(ValueError, match="requires evidence"):
        link(evidence_ids=())


def test_unmapped_is_explicit_not_synthetic() -> None:
    unmapped = link(
        object_id=None,
        association_method="unmapped",
        strength="none",
        evidence_ids=(),
    )
    record = unmapped.as_record()
    assert record["object"]["id"] is None
    assert record["confirmed"] is False
    assert AssociationLedger([unmapped]).partition()["unmapped"] == [record]


def test_many_to_many_records_are_deterministic_and_deduplicated() -> None:
    repository = link(
        object_kind="repository",
        object_id="repo-ael",
        association_method="user_confirmed",
        strength="medium",
        evidence_ids=("mapping:1",),
    )
    release = link(
        object_kind="release",
        object_id="v0.4.0",
        association_method="mapping_rule",
        strength="medium",
        evidence_ids=("manifest:1",),
    )
    first = AssociationLedger([release, repository, repository])
    second = AssociationLedger([repository, release])
    assert first.records() == second.records()
    assert len(first.records()) == 2
    assert first.links_for("session", "session-01") == tuple(
        sorted((release, repository), key=lambda item: item.link_id)
    )


def test_invalid_kind_method_and_explicit_strength_are_rejected() -> None:
    with pytest.raises(ValueError, match="subject kind"):
        link(subject_kind="provider")
    with pytest.raises(ValueError, match="association method"):
        link(association_method="proximity")
    with pytest.raises(ValueError, match="must be strong"):
        link(strength="medium")

