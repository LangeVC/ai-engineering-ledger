from __future__ import annotations

import json

import pytest

from export.privacy import (
    PrivacyProfile,
    PrivacyViolation,
    approved_artifacts,
    require_artifact_allowed,
    scan_artifacts,
)


PROFILE = PrivacyProfile.create(
    "study-public",
    ["claude-pro", "github-langevc", "repo-ael"],
)


@pytest.mark.parametrize(
    ("payload", "rule"),
    [
        ({"prompt": "summarize private work"}, "FORBIDDEN_FIELD"),
        ({"response": "private model output"}, "FORBIDDEN_FIELD"),
        ({"access_token": "not-echoed"}, "FORBIDDEN_FIELD"),
        ({"note": "Bearer super-secret-value"}, "SECRET_VALUE"),
        ({"owner": "private.person@example.com"}, "EMAIL_VALUE"),
        ({"source": "/Users/private/repository"}, "PRIVATE_PATH"),
        ({"remote": "ssh://private.internal/repository"}, "PRIVATE_REMOTE"),
        ({"raw_evidence": "base64-screenshot-or-transcript"}, "RAW_EVIDENCE"),
    ],
)
def test_canaries_block_without_echoing_values(payload, rule) -> None:
    report = scan_artifacts({"study.json": payload}, profile=PROFILE)

    assert report["blocked_artifacts"] == ["study.json"]
    result = report["artifacts"][0]
    assert rule in result["rule_ids"]
    rendered = json.dumps(report, sort_keys=True)
    for value in payload.values():
        assert str(value) not in rendered


def test_aliases_are_limited_to_selected_profile_and_scope() -> None:
    allowed = scan_artifacts(
        {
            "study.json": {
                "account_alias": "claude-pro",
                "repository_alias": "repo-ael",
            }
        },
        profile=PROFILE,
    )
    blocked = scan_artifacts(
        {"study.json": {"account_alias": "private-human-account"}},
        profile=PROFILE,
    )

    assert allowed["passed"] is True
    assert blocked["artifacts"][0]["rule_ids"] == ["UNAPPROVED_ALIAS"]


def test_failure_blocks_only_affected_artifact() -> None:
    artifacts = {
        "analysis.json": {"account_alias": "claude-pro", "value": 12},
        "snapshot.json": {"response": "never publish this"},
        "claims.json": {"repository_alias": "repo-ael", "value": 7},
    }
    report = scan_artifacts(artifacts, profile=PROFILE)

    assert report["allowed_artifacts"] == ["analysis.json", "claims.json"]
    assert report["blocked_artifacts"] == ["snapshot.json"]
    assert approved_artifacts(artifacts, report=report, profile=PROFILE) == {
        "analysis.json": artifacts["analysis.json"],
        "claims.json": artifacts["claims.json"],
    }
    require_artifact_allowed(
        report, "analysis.json", payload=artifacts["analysis.json"], profile=PROFILE
    )
    with pytest.raises(PrivacyViolation, match=r"snapshot\.json: FORBIDDEN_FIELD") as error:
        require_artifact_allowed(
            report, "snapshot.json", payload=artifacts["snapshot.json"], profile=PROFILE
        )
    assert "never publish this" not in str(error.value)


def test_raw_private_evidence_is_rejected_for_json_and_snapshot_bundles() -> None:
    artifacts = {
        "study.json": b'{"evidence_content":"private transcript"}',
        "snapshot.json": {"screenshot": "opaque-image-data"},
    }
    report = scan_artifacts(artifacts, profile=PROFILE)

    assert report["blocked_artifacts"] == ["snapshot.json", "study.json"]
    by_name = {item["artifact"]: item for item in report["artifacts"]}
    assert "RAW_EVIDENCE" in by_name["study.json"]["rule_ids"]
    assert "FORBIDDEN_FIELD" in by_name["snapshot.json"]["rule_ids"]


def test_scan_report_is_deterministic_and_contains_only_hashes_not_payloads() -> None:
    first = scan_artifacts(
        {"study.json": {"repository_alias": "repo-ael", "value": 1}},
        profile=PROFILE,
    )
    second = scan_artifacts(
        {"study.json": {"value": 1, "repository_alias": "repo-ael"}},
        profile=PROFILE,
    )

    assert first == second
    assert len(first["artifacts"][0]["content_sha256"]) == 64

    with pytest.raises(ValueError, match="privacy report verification failed"):
        approved_artifacts(
            {"study.json": {"repository_alias": "repo-ael", "value": 2}},
            report=first,
            profile=PROFILE,
        )


def test_plain_utf8_bytes_are_raw_evidence_but_canonical_json_bytes_are_scanned() -> None:
    raw = scan_artifacts({"transcript.json": b"plain private transcript"}, profile=PROFILE)
    scalar = scan_artifacts({"transcript.json": b'"plain private transcript"'}, profile=PROFILE)
    canonical = scan_artifacts(
        {"study.json": b'{"repository_alias":"repo-ael","value":1}'},
        profile=PROFILE,
    )
    noncanonical = scan_artifacts(
        {"study.json": b'{ "repository_alias": "repo-ael", "value": 1 }'},
        profile=PROFILE,
    )

    assert raw["artifacts"][0]["rule_ids"] == ["RAW_EVIDENCE"]
    assert scalar["artifacts"][0]["rule_ids"] == ["RAW_EVIDENCE"]
    assert canonical["passed"] is True
    assert noncanonical["artifacts"][0]["rule_ids"] == ["RAW_EVIDENCE"]


def test_caller_cannot_forge_allow_decision_or_swap_profile() -> None:
    artifacts = {"study.json": {"response": "private output"}}
    report = scan_artifacts(artifacts, profile=PROFILE)
    forged = json.loads(json.dumps(report))
    forged["artifacts"][0]["decision"] = "allow"
    forged["artifacts"][0]["rule_ids"] = []
    forged["blocked_artifacts"] = []
    forged["allowed_artifacts"] = ["study.json"]
    forged["passed"] = True

    with pytest.raises(ValueError, match="verification failed"):
        approved_artifacts(artifacts, report=forged, profile=PROFILE)
    with pytest.raises(ValueError, match="verification failed"):
        require_artifact_allowed(
            forged, "study.json", payload=artifacts["study.json"], profile=PROFILE
        )

    other = PrivacyProfile.create("other-profile", ["repo-ael"])
    with pytest.raises(ValueError, match="verification failed"):
        require_artifact_allowed(
            report, "study.json", payload=artifacts["study.json"], profile=other
        )


@pytest.mark.parametrize(
    "artifact",
    [
        "Bearer-secret-token.json",
        "sk-supersecretname.json",
        "private.person@example.com.json",
        "ssh://private.internal/repo",
    ],
)
def test_sensitive_artifact_names_are_rejected_before_report(artifact: str) -> None:
    with pytest.raises(ValueError, match="artifact name is sensitive") as error:
        scan_artifacts({artifact: {"value": 1}}, profile=PROFILE)
    assert artifact not in str(error.value)
