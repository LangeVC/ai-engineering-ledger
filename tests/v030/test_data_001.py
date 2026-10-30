import json

import pytest

from export.study import StudyExporter


def export_kwargs(timestamp):
    return {
        "filters": {"from": "2026-04-26", "to": "2026-11-01", "accounts": ["claude-pro"]},
        "timezone_name": "Europe/Berlin",
        "study_cutoff": "2026-11-01T00:00:00Z",
        "data_state": {"id": "ds-12345678", "as_of": "2026-11-01T00:00:00Z"},
        "source_status": [
            {"source_alias": "claude-pro", "account_alias": "claude-pro", "status": "partial"}
        ],
        "missing_periods": [
            {"source_alias": "claude-pro", "from": "2026-04-01", "to": "2026-04-26"}
        ],
        "evidence_hashes": [
            {"evidence_id": "ev-001", "sha256": "a" * 64, "reproducible": True}
        ],
        "export_timestamp": timestamp,
    }


def domain_rows():
    return {
        "resources": [
            {
                "resource_id": "claude-pro",
                "kind": "subscription",
                "state": "value",
                "value": "20.00",
                "unit": "USD/month",
                "collection_phase": "reconstructed",
            }
        ],
        "runtime": [
            {
                "request_id": "request-001",
                "state": "partial",
                "value": 120,
                "unit": "raw_token_activity",
                "collection_phase": "historical",
            },
            {
                "request_id": "request-002",
                "state": "value",
                "value": 12,
                "unit": "raw_token_activity",
                "collection_phase": "prospective",
            },
        ],
        "engineering": [
            {
                "repository_alias": "ai-engineering-ledger",
                "state": "value",
                "value": 3,
                "unit": "commit",
                "collection_phase": "reconstructed",
            }
        ],
        "products": {"status": "unverified", "records": []},
        "costs": [
            {"cost_id": "cost-001", "state": "value", "value": "20.00", "unit": "USD"}
        ],
        "coverage": {"status": "partial", "records": []},
        "reconciliation": [],
        "provenance": [
            {"record_id": "request-001", "evidence_id": "ev-001", "reproducible": True}
        ],
    }


def test_analysis_bundle_contains_versioned_domains_filters_gaps_and_transformations(tmp_path):
    destination = tmp_path / "export"
    StudyExporter(transformation_version="0.3.0").write(
        destination, domain_rows(), **export_kwargs("2026-10-29T12:00:00Z")
    )

    bundle = json.loads((destination / "analysis.json").read_text(encoding="utf-8"))
    manifest = json.loads((destination / "analysis-manifest.json").read_text(encoding="utf-8"))

    assert bundle["schema_version"] == "0.3"
    assert bundle["transformation_version"] == "0.3.0"
    assert bundle["filters"]["semantics"] == "[start,end_exclusive)"
    assert bundle["filters"]["start"] == "2026-04-26"
    assert bundle["filters"]["end_exclusive"] == "2026-11-01"
    assert bundle["timezone"] == "Europe/Berlin"
    assert bundle["analysis_cutoff"] == "2026-11-01T00:00:00Z"
    assert bundle["source_gaps"][0]["source_alias"] == "claude-pro"
    assert bundle["domains"]["products"] == {"status": "unverified", "records": []}
    assert set(bundle["domains"]) == {
        "resources",
        "runtime",
        "engineering",
        "products",
        "costs",
        "coverage",
        "reconciliation",
        "provenance",
    }
    assert manifest["files"][0]["name"] == "analysis.json"
    assert manifest["content_hash"] == manifest["files"][0]["sha256"]


def test_reconstructed_and_prospective_observations_remain_distinguishable(tmp_path):
    destination = tmp_path / "export"
    StudyExporter().write(destination, domain_rows(), **export_kwargs("2026-10-29T12:00:00Z"))
    bundle = json.loads((destination / "analysis.json").read_text(encoding="utf-8"))

    assert bundle["observation_phase_counts"]["reconstructed"] == 3
    assert bundle["observation_phase_counts"]["prospective"] == 1
    phases = {record.get("collection_phase") for record in bundle["domains"]["runtime"]["records"]}
    assert phases == {"historical", "prospective"}


def test_same_evidence_and_filter_has_stable_hash_despite_export_timestamp(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    exporter = StudyExporter(transformation_version="0.3.0")
    legacy_first = exporter.write(first, domain_rows(), **export_kwargs("2026-10-29T12:00:00Z"))
    legacy_second = exporter.write(second, domain_rows(), **export_kwargs("2026-10-30T12:00:00Z"))

    analysis_first = json.loads((first / "analysis-manifest.json").read_text(encoding="utf-8"))
    analysis_second = json.loads((second / "analysis-manifest.json").read_text(encoding="utf-8"))
    assert analysis_first["export_timestamp"] != analysis_second["export_timestamp"]
    assert analysis_first["content_hash"] == analysis_second["content_hash"]
    assert legacy_first["content_hash"] == legacy_second["content_hash"]
    assert (first / "analysis.json").read_bytes() == (second / "analysis.json").read_bytes()


@pytest.mark.parametrize("state", ["unknown", "unsupported", "unverified", "unavailable"])
def test_non_value_states_cannot_silently_encode_zero(state, tmp_path):
    rows = domain_rows()
    rows["products"] = {"status": "partial", "records": [{"state": state, "value": 0}]}
    with pytest.raises(ValueError, match="must not be represented as a numeric value"):
        StudyExporter().write(tmp_path / state, rows, **export_kwargs("2026-10-29T12:00:00Z"))


def test_missing_domains_are_explicitly_unavailable(tmp_path):
    destination = tmp_path / "export"
    StudyExporter().write(destination, {}, **export_kwargs("2026-10-29T12:00:00Z"))
    bundle = json.loads((destination / "analysis.json").read_text(encoding="utf-8"))

    assert all(domain == {"status": "unavailable", "records": []} for domain in bundle["domains"].values())


def test_nested_sensitive_content_is_rejected(tmp_path):
    rows = domain_rows()
    rows["runtime"][0]["details"] = {"prompt": "private request"}
    with pytest.raises(ValueError, match="forbidden analysis-export field"):
        StudyExporter().write(tmp_path / "unsafe", rows, **export_kwargs("2026-10-29T12:00:00Z"))


@pytest.mark.parametrize("canary", ["sk-supersecretvalue", "sk_supersecretvalue"])
def test_study_export_rejects_sk_secret_variants(canary, tmp_path):
    rows = domain_rows()
    rows["runtime"][0]["details"] = {"note": canary}
    with pytest.raises(ValueError, match="sensitive") as error:
        StudyExporter().write(
            tmp_path / "unsafe-secret",
            rows,
            **export_kwargs("2026-10-29T12:00:00Z"),
        )
    assert canary not in str(error.value)


@pytest.mark.parametrize(
    ("field", "canary"),
    [
        ("filters", "/Users/private/study.csv"),
        ("timezone_name", "operator@example.com"),
        ("study_cutoff", "Bearer secret-token-123"),
        ("export_id", "ssh://git@private.example/repository"),
        ("export_timestamp", "operator@example.com"),
        ("data_state", "/home/operator/private.json"),
        ("source_status", "git@private.example:org/repository.git"),
        ("missing_periods", "private@example.com"),
        ("confidence_distribution", "owner@example.com"),
        ("evidence_hashes", "https://user:password@private.example/repository.git"),
    ],
)
def test_all_emitted_metadata_is_privacy_scanned_before_writes(field, canary, tmp_path):
    destination = tmp_path / field
    kwargs = export_kwargs("2026-10-29T12:00:00Z")
    if field == "filters":
        kwargs[field] = {**kwargs[field], "canary": canary}
    elif field == "data_state":
        kwargs[field] = {**kwargs[field], "canary": canary}
    elif field == "source_status":
        kwargs[field] = [*kwargs[field], {"source_alias": "canary", "note": canary}]
    elif field == "missing_periods":
        kwargs[field] = [*kwargs[field], {"source_alias": "canary", "note": canary}]
    elif field == "confidence_distribution":
        kwargs[field] = {canary: 1}
    elif field == "evidence_hashes":
        kwargs[field] = [*kwargs[field], {"evidence_id": canary, "sha256": "b" * 64}]
    else:
        kwargs[field] = canary

    with pytest.raises(ValueError, match="sensitive") as error:
        StudyExporter().write(destination, domain_rows(), **kwargs)
    assert canary not in str(error.value)
    assert not destination.exists()


def test_metric_value_requires_explicit_state_and_evidenced_zero_is_preserved(tmp_path):
    missing_state = domain_rows()
    missing_state["products"] = {"status": "partial", "records": [{"value": 0, "unit": "item"}]}
    with pytest.raises(ValueError, match="requires an explicit state"):
        StudyExporter().write(
            tmp_path / "missing-state",
            missing_state,
            **export_kwargs("2026-10-29T12:00:00Z"),
        )

    explicit_zero = domain_rows()
    explicit_zero["products"] = {
        "status": "complete",
        "records": [{"state": "evidenced_zero", "value": 0, "unit": "item"}],
    }
    destination = tmp_path / "explicit-zero"
    StudyExporter().write(
        destination,
        explicit_zero,
        **export_kwargs("2026-10-29T12:00:00Z"),
    )
    bundle = json.loads((destination / "analysis.json").read_text(encoding="utf-8"))
    assert bundle["domains"]["products"]["records"] == [
        {"state": "evidenced_zero", "unit": "item", "value": 0}
    ]


def test_unordered_metadata_permutations_produce_identical_content_hashes(tmp_path):
    kwargs = export_kwargs("2026-10-29T12:00:00Z")
    kwargs["source_status"].append(
        {"source_alias": "codex-plus", "account_alias": "codex-plus", "status": "partial"}
    )
    kwargs["missing_periods"].append(
        {"source_alias": "codex-plus", "from": "2026-05-01", "to": "2026-06-01"}
    )
    kwargs["evidence_hashes"].append(
        {"evidence_id": "ev-002", "sha256": "b" * 64, "reproducible": True}
    )
    reversed_kwargs = {
        **kwargs,
        "source_status": list(reversed(kwargs["source_status"])),
        "missing_periods": list(reversed(kwargs["missing_periods"])),
        "evidence_hashes": list(reversed(kwargs["evidence_hashes"])),
    }

    first = StudyExporter().write(tmp_path / "first", domain_rows(), **kwargs)
    second = StudyExporter().write(tmp_path / "second", domain_rows(), **reversed_kwargs)
    assert first["content_hash"] == second["content_hash"]
    assert (tmp_path / "first" / "analysis.json").read_bytes() == (
        tmp_path / "second" / "analysis.json"
    ).read_bytes()
