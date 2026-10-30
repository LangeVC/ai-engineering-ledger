from __future__ import annotations

import copy
import hashlib

import pytest

from export.dashboard_snapshot import build_dashboard_snapshot, encode_dashboard_snapshot


DATASET_HASH = hashlib.sha256(b"study-dataset").hexdigest()


def _claim(claim_id: str, value="49.00", unit="EUR", status="partial"):
    return {
        "record_type": "claim",
        "schema_version": "0.3",
        "id": claim_id,
        "value": value,
        "unit": unit,
        "coverage": {
            "status": status,
            "sample_count": 3,
            "limitations": ["provider billing is partial"] if status == "partial" else [],
        },
    }


def _bundle(records=None):
    core = {
        "record_type": "study_bundle",
        "schema_version": "0.3",
        "transformation_version": "0.3.0",
        "claims": records or [_claim("claim.cost.total"), _claim("claim.output.loc", 1200, "line", "complete")],
        "correlations": [],
        "coverage": [],
        "warnings": [],
    }
    return {
        **core,
        "dataset_hash": DATASET_HASH,
        "content_hash": hashlib.sha256(
            __import__("json").dumps(core, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def _full_filter():
    return {
        "start": "2026-04-26T00:00:00+02:00",
        "end_exclusive": "2026-11-01T00:00:00+01:00",
        "providers": [],
        "accounts": [],
        "products": [],
        "repositories": [],
        "models": [],
        "capabilities": [],
    }


def _snapshot(**overrides):
    arguments = {
        "title": "Costs and engineering output",
        "active_filter": _full_filter(),
        "dataset_hash": DATASET_HASH,
        "claims_bundle": _bundle(),
        "claim_ids": ["claim.output.loc", "claim.cost.total"],
        "alt_text": "Total cost and changed lines for the selected period.",
        "approved_identifiers": [],
    }
    arguments.update(overrides)
    return build_dashboard_snapshot(**arguments)


def test_full_range_snapshot_derives_values_units_and_coverage_from_claims() -> None:
    snapshot = _snapshot()

    assert snapshot["trigger"] == "operator" and snapshot["optional"] is True
    assert snapshot["active_filter"]["boundary"] == "half_open"
    assert len(snapshot["claims_bundle_hash"]) == 64
    assert [(item["claim_id"], item["value"], item["unit"]) for item in snapshot["claims"]] == [
        ("claim.cost.total", "49.00", "EUR"),
        ("claim.output.loc", 1200, "line"),
    ]
    assert "claim.cost.total: partial" in snapshot["coverage_note"]
    assert snapshot["rendering"] == {"required": False, "format": "json-specification"}


def test_bounded_multidimensional_filter_is_fully_bound_and_private_safe() -> None:
    active_filter = {
        **_full_filter(),
        "start": "2026-10-01T00:00:00Z",
        "providers": ["provider:anthropic"],
        "accounts": ["account:claude-pro"],
        "products": ["product:ael"],
        "repositories": ["repo:ael"],
        "models": ["model:sonnet"],
        "capabilities": ["capability:code-generation"],
    }
    approved = [value for name in active_filter if name not in {"start", "end_exclusive"} for value in active_filter[name]]
    snapshot = _snapshot(active_filter=active_filter, approved_identifiers=approved)

    assert snapshot["active_filter"]["dimensions"]["repositories"] == ["repo:ael"]
    assert snapshot["filter_digest"] != _snapshot()["filter_digest"]


def test_filter_and_claim_permutations_are_deterministic() -> None:
    first = _snapshot()
    bundle = _bundle(list(reversed(_bundle()["claims"])))
    second = _snapshot(claims_bundle=bundle, claim_ids=list(reversed(["claim.output.loc", "claim.cost.total"])))

    assert first == second
    assert encode_dashboard_snapshot(first) == encode_dashboard_snapshot(second)


def test_tampered_values_and_bundle_or_dataset_hashes_are_rejected() -> None:
    bundle = _bundle()
    bundle["claims"][0]["value"] = "999"
    with pytest.raises(ValueError, match="content hash verification failed"):
        _snapshot(claims_bundle=bundle)

    wrong_dataset = copy.deepcopy(_bundle())
    wrong_dataset["dataset_hash"] = "b" * 64
    with pytest.raises(ValueError, match="not bound"):
        _snapshot(claims_bundle=wrong_dataset)

    untyped = _bundle([_claim("claim.cost.total", {"amount": 49})])
    with pytest.raises(ValueError, match="numeric"):
        _snapshot(claims_bundle=untyped, claim_ids=["claim.cost.total"])


def test_omitted_or_private_filter_dimensions_are_rejected() -> None:
    omitted = _full_filter()
    omitted.pop("models")
    with pytest.raises(ValueError, match="omits dimensions"):
        _snapshot(active_filter=omitted)

    private = {**_full_filter(), "repositories": ["/Users/private/repo"]}
    with pytest.raises(ValueError, match="invalid identifiers|private absolute path"):
        _snapshot(active_filter=private, approved_identifiers=["/Users/private/repo"])

    secret = {**_full_filter(), "accounts": ["Bearer-secret-token"]}
    with pytest.raises(ValueError, match="invalid identifiers|secret-like"):
        _snapshot(active_filter=secret, approved_identifiers=["Bearer-secret-token"])


@pytest.mark.parametrize(
    ("title", "message"),
    [
        ("private.person@example.com", "email address"),
        ("ssh://private.internal/repository", "private remote"),
    ],
)
def test_snapshot_rejects_email_and_private_remote(title: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _snapshot(title=title)
