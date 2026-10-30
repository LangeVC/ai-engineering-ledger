from __future__ import annotations

import copy
import hashlib
import json

import pytest

from export.claims import generate_claim_registry
from export.study_json import build_study_json, encode_study_json, normalize_study_filter


FILTER_DIMENSIONS = {
    "providers": ["claude", "codex"],
    "repositories": ["ael", "faigate"],
}


def _analysis(start="2026-10-01", end="2026-11-01"):
    return {
        "schema_version": "0.3",
        "contract": "ael.study.analysis",
        "transformation_version": "0.3.0",
        "filters": {
            "start": start,
            "end_exclusive": end,
            "semantics": "[start,end_exclusive)",
            **FILTER_DIMENSIONS,
        },
        "timezone": "Europe/Berlin",
        "analysis_cutoff": "2026-11-01T00:00:00Z",
        "data_state": {"id": "dataset-v1"},
        "source_status": [],
        "source_gaps": [{"source": "claude-pro", "reason": "provider history is partial"}],
        "transformations": [],
        "observation_phase_counts": {"reconstructed": 2, "prospective": 0, "unknown": 0},
        "evidence_hashes": [{"evidence_id": "ev-cost", "sha256": "a" * 64}],
        "domains": {
            "resources": {"status": "complete", "records": []},
            "runtime": {
                "status": "partial",
                "records": [
                    {
                        "period": "2026-10",
                        "state": "partial",
                        "value": 100,
                        "unit": "raw_token_activity",
                        "evidence_ids": ["ev-runtime"],
                        "warnings": ["local logs only"],
                    }
                ],
            },
            "engineering": {
                "status": "complete",
                "records": [
                    {
                        "period": "2026-10",
                        "state": "value",
                        "value": 12,
                        "unit": "commit",
                        "evidence_ids": ["ev-git"],
                    }
                ],
            },
            "products": {"status": "unknown", "records": []},
            "costs": {
                "status": "complete",
                "records": [
                    {
                        "period": "2026-10",
                        "state": "value",
                        "value": 49,
                        "unit": "EUR",
                        "evidence_ids": ["ev-cost"],
                    }
                ],
            },
            "coverage": {"status": "complete", "records": []},
            "reconciliation": {"status": "complete", "records": []},
            "provenance": {"status": "complete", "records": []},
        },
    }


def _claims(analysis):
    return generate_claim_registry(
        analysis,
        claim_specs=[
            {"name": "cost-total", "domain": "costs", "operation": "sum", "unit": "EUR"},
            {"name": "runtime-total", "domain": "runtime", "operation": "sum", "unit": "raw_token_activity"},
            {"name": "unknown-products", "domain": "products", "operation": "sum", "unit": "product"},
        ],
        correlation_specs=[],
    )


def _filter(start="2026-10-01", end="2026-11-01"):
    return {"start": start, "end_exclusive": end, **FILTER_DIMENSIONS}


def test_export_contains_data_contract_claims_coverage_and_provenance():
    analysis = _analysis()
    result = build_study_json(
        analysis_bundle=analysis,
        claims_bundle=_claims(analysis),
        active_filter=_filter(),
    )

    assert result["schemas"]["export"] == "ael.study.data-export@0.3"
    assert result["active_filter"]["semantics"] == "[start,end_exclusive)"
    assert result["active_filter"]["dimensions"] == FILTER_DIMENSIONS
    assert {claim["unit"] for claim in result["claims"]} == {"EUR", "product", "raw_token_activity"}
    assert any(claim["coverage"]["status"] == "partial" for claim in result["claims"])
    assert set(result["provenance"]["evidence_ids"]) == {"ev-cost", "ev-runtime"}
    assert "provider history is partial" in result["warnings"]
    assert len(result["content_hash"]) == 64
    assert not any(key in result for key in ("article", "website", "social", "seo"))


def test_unknown_and_partial_states_are_not_coerced_to_zero():
    analysis = _analysis()
    result = build_study_json(
        analysis_bundle=analysis,
        claims_bundle=_claims(analysis),
        active_filter=_filter(),
    )
    by_unit = {claim["unit"]: claim for claim in result["claims"]}

    assert by_unit["product"]["value"] is None
    assert by_unit["product"]["value_state"] == "unknown"
    assert by_unit["raw_token_activity"]["value"] == "100"
    assert by_unit["raw_token_activity"]["value_state"] == "partial"


@pytest.mark.parametrize("canary", ["sk-supersecretvalue", "sk_supersecretvalue"])
def test_study_json_rejects_sk_secret_variants(canary):
    analysis = _analysis()
    claims = _claims(analysis)
    analysis["source_gaps"][0]["reason"] = canary

    with pytest.raises(ValueError, match="sensitive") as error:
        build_study_json(
            analysis_bundle=analysis,
            claims_bundle=claims,
            active_filter=_filter(),
        )
    assert canary not in str(error.value)


def test_active_filter_must_match_analysis_and_claim_scope():
    analysis = _analysis()
    with pytest.raises(ValueError, match="active filter does not match"):
        build_study_json(
            analysis_bundle=analysis,
            claims_bundle=_claims(analysis),
            active_filter=_filter("2026-10-15"),
        )

    other = _analysis("2026-09-01", "2026-10-01")
    with pytest.raises(ValueError, match="claims bundle scope does not match"):
        build_study_json(
            analysis_bundle=analysis,
            claims_bundle=_claims(other),
            active_filter=_filter(),
        )


def test_bundle_and_each_analytic_scope_must_bind_all_filter_dimensions():
    analysis = _analysis()
    claims = _claims(analysis)
    claims["scope"]["dimensions"]["filter"]["providers"] = ["other-provider"]
    core = {key: value for key, value in claims.items() if key != "content_hash"}
    claims["content_hash"] = hashlib.sha256(
        json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with pytest.raises(ValueError, match="claims bundle scope does not match"):
        build_study_json(
            analysis_bundle=analysis,
            claims_bundle=claims,
            active_filter=_filter(),
        )

    claims = _claims(analysis)
    claims["claims"][0]["scope"]["dimensions"]["filter"]["repositories"] = ["other"]
    core = {key: value for key, value in claims.items() if key != "content_hash"}
    claims["content_hash"] = hashlib.sha256(
        json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with pytest.raises(ValueError, match="analytic record .* does not match"):
        build_study_json(
            analysis_bundle=analysis,
            claims_bundle=claims,
            active_filter=_filter(),
        )


def test_same_data_filter_and_permutations_are_byte_identical():
    analysis = _analysis()
    permuted_analysis = copy.deepcopy(analysis)
    permuted_analysis["evidence_hashes"].reverse()
    permuted_analysis["domains"]["runtime"]["records"][0]["evidence_ids"] = [
        "ev-runtime-extra",
        "ev-runtime",
    ]
    analysis["domains"]["runtime"]["records"][0]["evidence_ids"] = [
        "ev-runtime",
        "ev-runtime-extra",
    ]
    permuted_analysis["domains"]["runtime"]["records"][0]["warnings"] = [
        "second warning",
        "local logs only",
    ]
    analysis["domains"]["runtime"]["records"][0]["warnings"] = [
        "local logs only",
        "second warning",
    ]
    claims = _claims(analysis)
    for claim in claims["claims"]:
        if claim["coverage"]["status"] == "partial":
            claim["coverage"]["warning_codes"].append("SECOND_PARTIAL_WARNING")
            claim["coverage"]["limitations"].append("second limitation")
            claim["warnings"].append("second limitation")
    core = {key: value for key, value in claims.items() if key != "content_hash"}
    claims["content_hash"] = hashlib.sha256(
        json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    first = build_study_json(
        analysis_bundle=analysis,
        claims_bundle=claims,
        active_filter=_filter(),
    )
    permuted_claims = copy.deepcopy(claims)
    permuted_claims["claims"].reverse()
    for claim in permuted_claims["claims"]:
        claim["evidence_ids"].reverse()
        claim["warnings"].reverse()
        claim["coverage"]["warning_codes"].reverse()
        claim["coverage"]["limitations"].reverse()
    core = {key: value for key, value in permuted_claims.items() if key != "content_hash"}
    permuted_claims["content_hash"] = hashlib.sha256(
        json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    second = build_study_json(
        analysis_bundle=permuted_analysis,
        claims_bundle=permuted_claims,
        active_filter={
            "repositories": ["faigate", "ael"],
            "end_exclusive": "2026-11-01",
            "providers": ["codex", "claude"],
            "start": "2026-10-01",
        },
    )

    assert encode_study_json(first) == encode_study_json(second)


def test_list_valued_filter_dimensions_are_semantic_sets():
    forward = normalize_study_filter(_filter(), "Europe/Berlin")
    reversed_dimensions = normalize_study_filter(
        {
            "start": "2026-10-01",
            "end_exclusive": "2026-11-01",
            "providers": ["codex", "claude"],
            "repositories": ["faigate", "ael"],
        },
        "Europe/Berlin",
    )

    assert forward == reversed_dimensions

    different = normalize_study_filter(
        {
            **_filter(),
            "providers": ["claude", "other-provider"],
        },
        "Europe/Berlin",
    )
    assert forward != different


def test_private_values_and_tampered_hashes_are_rejected():
    analysis = _analysis()
    analysis["source_gaps"][0]["reason"] = "/Users/private/evidence.json"
    with pytest.raises(ValueError, match="sensitive"):
        build_study_json(
            analysis_bundle=analysis,
            claims_bundle=_claims(_analysis()),
            active_filter=_filter(),
        )

    claims = _claims(_analysis())
    claims["claims"][0]["value"] = "999"
    with pytest.raises(ValueError, match="content hash verification failed"):
        build_study_json(
            analysis_bundle=_analysis(),
            claims_bundle=claims,
            active_filter=_filter(),
        )
