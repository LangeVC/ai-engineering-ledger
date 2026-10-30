import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from export.claims import generate_claim_registry


ROOT = Path(__file__).resolve().parents[2]


def analysis_bundle():
    return {
        "schema_version": "0.3",
        "contract": "ael.study.analysis",
        "transformation_version": "0.3.0",
        "filters": {
            "start": "2026-04-26",
            "end_exclusive": "2026-11-01",
            "semantics": "[start,end_exclusive)",
        },
        "timezone": "Europe/Berlin",
        "analysis_cutoff": "2026-11-01T00:00:00Z",
        "data_state": {"id": "ds-12345678", "as_of": "2026-11-01T00:00:00Z"},
        "source_status": [],
        "source_gaps": [],
        "transformations": [],
        "observation_phase_counts": {"reconstructed": 6, "prospective": 0, "unknown": 0},
        "evidence_hashes": [{"evidence_id": "evidence-global", "sha256": "a" * 64}],
        "domains": {
            "resources": {"status": "complete", "records": []},
            "runtime": {
                "status": "partial",
                "records": [
                    {
                        "period": "2026-08",
                        "state": "value",
                        "value": 100,
                        "unit": "raw_token_activity",
                        "evidence_ids": ["runtime-aug"],
                        "warnings": ["local logs only"],
                    },
                    {
                        "period": "2026-09",
                        "state": "value",
                        "value": 200,
                        "unit": "raw_token_activity",
                        "evidence_ids": ["runtime-sep"],
                    },
                    {
                        "period": "2026-10",
                        "state": "partial",
                        "value": 300,
                        "unit": "raw_token_activity",
                        "evidence_ids": ["runtime-oct"],
                        "warnings": ["October is partial"],
                    },
                ],
            },
            "engineering": {
                "status": "complete",
                "records": [
                    {"period": "2026-08", "state": "value", "value": 10, "unit": "commit", "evidence_ids": ["git-aug"]},
                    {"period": "2026-09", "state": "value", "value": 20, "unit": "commit", "evidence_ids": ["git-sep"]},
                    {"period": "2026-10", "state": "value", "value": 30, "unit": "commit", "evidence_ids": ["git-oct"]},
                ],
            },
            "products": {"status": "unknown", "records": []},
            "costs": {"status": "complete", "records": []},
            "coverage": {"status": "complete", "records": []},
            "reconciliation": {"status": "complete", "records": []},
            "provenance": {"status": "complete", "records": []},
        },
    }


def claim_spec():
    return {
        "name": "raw-token-activity-total",
        "domain": "runtime",
        "operation": "sum",
        "field": "value",
        "unit": "raw_token_activity",
        "formula_version": "1",
    }


def correlation_spec():
    return {
        "name": "runtime-vs-commits",
        "left_domain": "runtime",
        "right_domain": "engineering",
        "left_dimension": "raw_token_activity",
        "right_dimension": "commit_count",
        "join_key": "period",
    }


def schema_validator():
    schema = json.loads((ROOT / "schema" / "study-v0.3.schema.json").read_text(encoding="utf-8"))
    return Draft202012Validator(schema, format_checker=FormatChecker())


def test_claims_and_correlations_are_complete_non_causal_and_schema_valid():
    registry = generate_claim_registry(
        analysis_bundle(), claim_specs=[claim_spec()], correlation_specs=[correlation_spec()]
    )
    claim = registry["claims"][0]
    correlation = registry["correlations"][0]

    assert claim["value"] == "600"
    assert claim["coverage"]["status"] == "partial"
    assert claim["confidence"]["level"] == "medium"
    assert claim["scope"]["interval"]["semantics"] == "[start,end_exclusive)"
    assert claim["evidence_ids"] == ["runtime-aug", "runtime-oct", "runtime-sep"]
    assert correlation["sample_size"] == 3
    assert correlation["coverage"]["sample_count"] == 3
    assert correlation["value"] == "1"
    assert correlation["causality_assertion"] is False
    assert "non-causal" in " ".join(correlation["warnings"])

    validator = schema_validator()
    validator.validate(claim)
    validator.validate(correlation)
    validator.validate(registry)


def test_registry_recomputes_deterministically_from_same_dataset():
    first = generate_claim_registry(
        analysis_bundle(), claim_specs=[claim_spec()], correlation_specs=[correlation_spec()]
    )
    second = generate_claim_registry(
        copy.deepcopy(analysis_bundle()), claim_specs=[copy.deepcopy(claim_spec())], correlation_specs=[copy.deepcopy(correlation_spec())]
    )

    assert first == second
    assert json.dumps(first, sort_keys=True, separators=(",", ":")) == json.dumps(
        second, sort_keys=True, separators=(",", ":")
    )


def test_unknown_status_is_preserved_as_null_claim_not_zero():
    spec = {"name": "unknown-products", "domain": "products", "operation": "sum", "unit": "product"}
    registry = generate_claim_registry(analysis_bundle(), claim_specs=[spec], correlation_specs=[])
    claim = registry["claims"][0]

    assert claim["value"] is None
    assert claim["coverage"]["status"] == "unknown"
    assert claim["confidence"]["level"] == "unknown"
    assert claim["evidence_ids"] == ["evidence-global"]


def test_incompatible_units_cannot_be_summed():
    bundle = analysis_bundle()
    bundle["domains"]["runtime"]["records"][1]["unit"] = "request"
    with pytest.raises(ValueError, match="incompatible units"):
        generate_claim_registry(bundle, claim_specs=[claim_spec()], correlation_specs=[])


def test_raw_tokens_cannot_be_converted_to_subscription_allowance():
    spec = {**claim_spec(), "unit": "subscription_allowance"}
    with pytest.raises(ValueError, match="cannot be converted to subscription allowance"):
        generate_claim_registry(analysis_bundle(), claim_specs=[spec], correlation_specs=[])


def test_provider_cooccurrence_cannot_attribute_engineering_output():
    spec = {
        **correlation_spec(),
        "left_dimension": "provider_claude",
        "right_dimension": "engineering_output",
    }
    with pytest.raises(ValueError, match="cannot attribute engineering output to a provider"):
        generate_claim_registry(analysis_bundle(), claim_specs=[], correlation_specs=[spec])


def test_correlation_cannot_request_causality():
    spec = {**correlation_spec(), "causality_assertion": True}
    with pytest.raises(ValueError, match="must not assert causality"):
        generate_claim_registry(analysis_bundle(), claim_specs=[], correlation_specs=[spec])
