import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "schema" / "study-v0.3.schema.json"


def validator():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def scope():
    return {
        "scope_id": "first-study",
        "scope_version": 1,
        "interval": {
            "start": "2026-04-26T00:00:00+02:00",
            "end_exclusive": "2026-11-01T00:00:00+01:00",
            "timezone": "Europe/Berlin",
            "semantics": "[start,end_exclusive)",
        },
        "dimensions": {"product": "ael"},
    }


def analytic_record(record_type="claim", schema_version="0.3"):
    record = {
        "record_type": record_type,
        "schema_version": schema_version,
        "id": f"{record_type}-001",
        "formula": {
            "version": "1",
            "registry_id": "sum.same-unit.v1",
            "expression": "sum(value)",
            "unit_contract": {
                "operand_units": ["request"],
                "output_unit": "request",
                "compatible": True,
            },
        },
        "scope": scope(),
        "evidence_ids": ["evidence-001"],
        "coverage": {
            "status": "partial",
            "observed_from": "2026-04-26T00:00:00+02:00",
            "observed_to_exclusive": "2026-10-29T00:00:00+01:00",
            "sample_count": 42,
            "limitations": ["provider dashboard history is incomplete"],
            "warning_codes": ["PARTIAL_COVERAGE"],
        },
        "confidence": {"level": "medium", "method": "source coverage assessment v1"},
        "unit": "request",
        "value": 42,
        "value_state": "partial",
        "causality_assertion": False,
        "provider_attribution": False,
        "warnings": ["partial coverage"],
    }
    if record_type == "correlation":
        record.pop("provider_attribution")
        record.update(
            {
                "left_dimension": "raw_token_activity",
                "right_dimension": "commits",
                "sample_size": 12,
                "causality_assertion": False,
            }
        )
    return record


def adapter_record(comparison="new", action="insert", supersedes=None):
    return {
        "record_type": "adapter_record",
        "schema_version": "0.3",
        "record_id": "claude-request-001",
        "source": {
            "provider": "claude",
            "surface": "claude-code",
            "entitlement": "subscription",
            "plan": "pro",
            "account_alias": "claude-pro",
            "auth_mode": "local_session",
        },
        "semantic_family": "raw_token_activity",
        "metric": "input_tokens",
        "value": 123,
        "unit": "token",
        "billing_assertion": False,
        "observed_at": "2026-10-29T12:00:00+01:00",
        "evidence_ids": ["evidence-001"],
        "revision": {
            "native_id": "request-001",
            "native_revision": 1 if comparison != "content_changed" else 2,
            "content_hash": "a" * 64,
            "comparison": comparison,
            "action": action,
            "supersedes": supersedes,
        },
    }


@pytest.mark.parametrize("schema_version", ["0.2", "0.3"])
def test_v02_and_v03_claim_envelopes_remain_readable(schema_version):
    validator().validate(analytic_record(schema_version=schema_version))


def test_claims_and_correlations_require_formula_scope_evidence_coverage_confidence():
    check = validator()
    check.validate(analytic_record("claim"))
    check.validate(analytic_record("correlation"))

    for required in ("formula", "scope", "evidence_ids", "coverage", "confidence"):
        invalid = analytic_record("correlation")
        invalid.pop(required)
        assert not check.is_valid(invalid), required


def test_raw_token_activity_can_never_assert_billing():
    check = validator()
    record = adapter_record()
    check.validate(record)
    record["billing_assertion"] = True
    assert not check.is_valid(record)


def test_exact_repeats_deduplicate_and_changed_content_creates_revision():
    check = validator()
    check.validate(adapter_record("exact_repeat", "deduplicate"))
    check.validate(adapter_record("content_changed", "create_revision", "revision-001"))

    assert not check.is_valid(adapter_record("exact_repeat", "create_revision", "revision-001"))
    assert not check.is_valid(adapter_record("content_changed", "deduplicate", "revision-001"))


def test_correlation_contract_forbids_causality_assertion():
    check = validator()
    record = analytic_record("correlation")
    check.validate(record)
    causal = copy.deepcopy(record)
    causal["causality_assertion"] = True
    assert not check.is_valid(causal)
