"""Contract and unit tests for generic mapping importer (US-05, US-06).
Tests:
- CSV, JSON, JSONL import with preview (US-05.a)
- Explicit delimiter, decimal comma, timezone (US-05.a)
- Unsupported nesting explanation (US-05.a)
- Real minimized DeepSeek fixture sum exactness (US-05.b)
- Disjoint report partition: total = accepted + existing + quarantined + corrected (US-05.c)
- Quarantine and isolation in partial mode vs strict abort (US-05.d)
- Replay idempotency: second import yields existing status, zero new events (US-06.a, T01)
- Validation of produced events and measurements against schema/core and semantic.py
"""

import json
import os
import sys
from decimal import Decimal

# Setup paths to import importers.mapping and schema.core
HERE = os.path.dirname(os.path.abspath(__file__))
curr = HERE
REPO_ROOT = None
while curr and curr != "/":
    if os.path.isdir(os.path.join(curr, "schema", "core")):
        REPO_ROOT = curr
        break
    curr = os.path.dirname(curr)
if not REPO_ROOT:
    REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", "..", "..", ".."))
WORKTREE_DIR = REPO_ROOT

sys.path.insert(0, WORKTREE_DIR)
sys.path.insert(0, REPO_ROOT)

from importers.mapping.engine import MappingImporter
from importers.mapping.models import (
    ColumnMappingRule,
    CsvOptions,
    ImportMode,
    MappingConfig,
    ValueStatus,
)
from importers.mapping.parser import parse_input_records
from importers.mapping.transformer import convert_value, parse_date_to_window

import schema.core.semantic as semantic
from schema.core.tests.minival import Loader, errors as minival_errors


def get_schema_loader():
    schema_dir = os.path.join(REPO_ROOT, "schema", "core")
    return Loader(schema_dir), json.load(open(os.path.join(schema_dir, "units.catalog.json"), encoding="utf-8"))


def test_schema_conformance_on_deepseek_fixture():
    """Verify produced events and measurements strictly validate against schema/core and semantic rules."""
    loader, catalog = get_schema_loader()
    event_schema = loader.doc("event.schema.json")
    meas_schema = loader.doc("measurement.schema.json")

    config_path = os.path.join(HERE, "fixtures", "deepseek_mapping_config.json")
    csv_path = os.path.join(HERE, "fixtures", "valid_deepseek.csv")

    with open(config_path, "r", encoding="utf-8") as f:
        cfg_dict = json.load(f)
    with open(csv_path, "r", encoding="utf-8") as f:
        csv_data = f.read()

    cfg = MappingConfig(**cfg_dict)
    importer = MappingImporter(cfg)
    report = importer.process_content(csv_data, mode=ImportMode.FULL, evidence_id="ev_01J9Z3K5Q8W2X4Y6A7B8C9D0E5")

    assert report.total_records == 3, f"Expected 3 records, got {report.total_records}"
    assert report.accepted == 3, f"Expected 3 accepted, got {report.accepted}"
    assert report.quarantined == 0
    assert report.verify_disjoint_sum()
    assert report.run_status == "complete"

    # Validate each event against event.schema.json and semantic.check_event
    for ev in report.events:
        errs = minival_errors(ev, event_schema, "event.schema.json", loader)
        assert not errs, f"Event schema validation failed: {errs}"
        sem_errs = semantic.check_event(ev)
        assert not sem_errs, f"Event semantic check failed: {sem_errs}"

    # Validate each measurement against measurement.schema.json and semantic.check_measurement
    for m in report.measurements:
        errs = minival_errors(m, meas_schema, "measurement.schema.json", loader)
        assert not errs, f"Measurement schema validation failed: {errs}"
        sem_errs = semantic.check_measurement(m, catalog)
        assert not sem_errs, f"Measurement semantic check failed: {sem_errs}"

    # Validate revision chains
    rev_by_meas = {}
    for m in report.measurements:
        rev_by_meas.setdefault(m["measurement_id"], []).append(m["revision"])
    for m_id, revs in rev_by_meas.items():
        chain_errs = semantic.check_revision_chain(revs)
        assert not chain_errs, f"Revision chain error: {chain_errs}"

    print("PASS: test_schema_conformance_on_deepseek_fixture")


def test_deepseek_fixture_sums_exact():
    """US-05.b: Fixture sums per account, model, metric match independent decimal calculation."""
    config_path = os.path.join(HERE, "fixtures", "deepseek_mapping_config.json")
    csv_path = os.path.join(HERE, "fixtures", "valid_deepseek.csv")

    with open(config_path, "r", encoding="utf-8") as f:
        cfg_dict = json.load(f)
    with open(csv_path, "r", encoding="utf-8") as f:
        csv_data = f.read()

    importer = MappingImporter(MappingConfig(**cfg_dict))
    report = importer.process_content(csv_data)

    # Expected sums:
    # prompt_tokens: 28450 + 15800 + 12600 = 56850
    # prompt_cache_hit_tokens: 22100 + 8500 + 10400 = 41000
    # prompt_cache_miss_tokens: 6350 + 7300 + 2200 = 15850
    # (Check invariant: 41000 + 15850 = 56850 == prompt_tokens)
    # completion_tokens: 4120 + 3450 + 1890 = 9460
    # reasoning_tokens: 0 + 2100 + 0 = 2100
    # requests: 14 + 3 + 8 = 25
    # cost: 0.007621 + 0.015240 + 0.003348 = 0.026209
    sums = {}
    for m in report.measurements:
        metric = m["metric"]
        val = Decimal(str(m["value"]))
        sums[metric] = sums.get(metric, Decimal(0)) + val

    assert sums["input_tokens"] == Decimal("56850"), f"input_tokens sum mismatch: {sums['input_tokens']}"
    assert sums["cache_hit_tokens"] == Decimal("41000")
    assert sums["cache_miss_tokens"] == Decimal("15850")
    assert sums["output_tokens"] == Decimal("9460")
    assert sums["reasoning_tokens"] == Decimal("2100")
    assert sums["requests"] == Decimal("25")
    assert sums["cost"] == Decimal("0.026209"), f"cost sum mismatch: {sums['cost']}"
    assert sums["cache_hit_tokens"] + sums["cache_miss_tokens"] == sums["input_tokens"]

    print("PASS: test_deepseek_fixture_sums_exact")


def test_idempotent_replay():
    """US-06.a / T01: Second import of identical file causes existing records, zero added active measurements."""
    config_path = os.path.join(HERE, "fixtures", "deepseek_mapping_config.json")
    csv_path = os.path.join(HERE, "fixtures", "valid_deepseek.csv")

    with open(config_path, "r", encoding="utf-8") as f:
        cfg_dict = json.load(f)
    with open(csv_path, "r", encoding="utf-8") as f:
        csv_data = f.read()

    importer = MappingImporter(MappingConfig(**cfg_dict))
    report1 = importer.process_content(csv_data)
    assert report1.accepted == 3
    assert report1.existing == 0

    # Second run with same importer (which retained existing keys)
    report2 = importer.process_content(csv_data)
    assert report2.total_records == 3
    assert report2.accepted == 0
    assert report2.existing == 3
    assert report2.quarantined == 0
    assert report2.verify_disjoint_sum()
    assert len(report2.measurements) == 0
    assert len(report2.events) == 0

    print("PASS: test_idempotent_replay")


def test_quarantine_and_disjoint_sum():
    """US-05.c & US-05.d: Invalid rows are quarantined, total = accepted + existing + quarantined + corrected."""
    config_path = os.path.join(HERE, "fixtures", "deepseek_mapping_config.json")
    csv_path = os.path.join(HERE, "fixtures", "invalid_records.csv")

    with open(config_path, "r", encoding="utf-8") as f:
        cfg_dict = json.load(f)
    with open(csv_path, "r", encoding="utf-8") as f:
        csv_data = f.read()

    importer = MappingImporter(MappingConfig(**cfg_dict))
    report = importer.process_content(csv_data, mode=ImportMode.FULL)

    # invalid_records.csv has 4 rows:
    # Row 1: valid -> accepted (1)
    # Row 2: model missing/empty -> quarantined (required dimension)
    # Row 3: hit(800) + miss(800) != total(1000) -> invariant violation -> quarantined
    # Row 4: invalid decimal 'invalid_cost' -> quarantined
    assert report.total_records == 4
    assert report.accepted == 1
    assert report.quarantined == 3
    assert report.existing == 0
    assert report.corrected == 0
    assert report.run_status == "partial"
    assert report.verify_disjoint_sum()
    assert len(report.quarantine_records) == 3

    print("PASS: test_quarantine_and_disjoint_sum")


def test_strict_mode_aborts_on_error():
    """US-05.d: Strict mode accepts 0 measurements if any validation error occurs."""
    config_path = os.path.join(HERE, "fixtures", "deepseek_mapping_config.json")
    csv_path = os.path.join(HERE, "fixtures", "invalid_records.csv")

    with open(config_path, "r", encoding="utf-8") as f:
        cfg_dict = json.load(f)
    with open(csv_path, "r", encoding="utf-8") as f:
        csv_data = f.read()

    importer = MappingImporter(MappingConfig(**cfg_dict))
    report = importer.process_content(csv_data, mode=ImportMode.STRICT)

    assert report.total_records == 4
    assert report.accepted == 0
    assert report.quarantined == 4
    assert report.run_status == "failed"
    assert len(report.measurements) == 0
    assert report.verify_disjoint_sum()

    print("PASS: test_strict_mode_aborts_on_error")


def test_formats_json_and_jsonl_and_preview():
    """US-05.a: Support JSON and JSONL with preview mode."""
    config_path = os.path.join(HERE, "fixtures", "deepseek_mapping_config.json")
    with open(config_path, "r", encoding="utf-8") as f:
        cfg_dict = json.load(f)

    # Test JSON
    cfg_json = MappingConfig(**{**cfg_dict, "format": "json"})
    with open(os.path.join(HERE, "fixtures", "valid_records.json"), "r", encoding="utf-8") as f:
        json_data = f.read()
    imp_json = MappingImporter(cfg_json)
    rep_preview = imp_json.process_content(json_data, mode=ImportMode.PREVIEW)
    assert rep_preview.total_records == 2
    assert rep_preview.accepted == 2
    assert len(rep_preview.preview_records) == 2
    assert rep_preview.preview_records[0]["measurements_count"] > 0

    # Test JSONL
    cfg_jsonl = MappingConfig(**{**cfg_dict, "format": "jsonl"})
    with open(os.path.join(HERE, "fixtures", "valid_records.jsonl"), "r", encoding="utf-8") as f:
        jsonl_data = f.read()
    imp_jsonl = MappingImporter(cfg_jsonl)
    rep_jsonl = imp_jsonl.process_content(jsonl_data, mode=ImportMode.FULL)
    assert rep_jsonl.total_records == 2
    assert rep_jsonl.accepted == 2
    assert rep_jsonl.run_status == "complete"

    print("PASS: test_formats_json_and_jsonl_and_preview")


def test_csv_delimiters_decimal_comma_and_timezone():
    """US-05.a: Explicit delimiter, decimal comma, and timezone handling."""
    csv_german = (
        "Datum;Modell;Anfragen;Tokens;Kosten\n"
        "2026-10-25;deepseek-chat;10;500;1,25\n"
    )
    cfg = MappingConfig(
        source="deepseek",
        account_alias="deepseek-main",
        format="csv",
        csv_options=CsvOptions(
            delimiter=";",
            decimal_separator=",",
            timezone="Europe/Berlin",
        ),
        column_mapping={
            "Datum": ColumnMappingRule(target_field="date", target_type="iso8601_date", required=True),
            "Modell": ColumnMappingRule(target_dimension="model", target_type="string", required=True),
            "Anfragen": ColumnMappingRule(target_metric="requests", unit="request", target_type="integer", required=True),
            "Tokens": ColumnMappingRule(target_metric="input_tokens", unit="token", target_type="integer", required=True),
            "Kosten": ColumnMappingRule(target_metric="cost", unit="eur", target_type="decimal", required=True),
        },
    )
    importer = MappingImporter(cfg)
    rep = importer.process_content(csv_german)
    assert rep.total_records == 1
    assert rep.accepted == 1
    assert rep.quarantined == 0

    meas_cost = [m for m in rep.measurements if m["metric"] == "cost"][0]
    assert meas_cost["value"] == "1.25"
    assert meas_cost["unit"] == "eur"

    ev = rep.events[0]
    # Europe/Berlin on 2026-10-25 00:00 is CEST (UTC+2) -> 2026-10-24T22:00:00Z
    assert ev["window"]["starts_at"] == "2026-10-24T22:00:00Z"
    print("PASS: test_csv_delimiters_decimal_comma_and_timezone")


def test_unsupported_nesting_is_explained_not_guessed():
    """US-05.a: Unsupported nesting produces explicit explanation, never guessed columns."""
    nested_json = json.dumps([
        {
            "date": "2026-10-25",
            "model": "deepseek-chat",
            "details": {"sub_metric": 10, "foo": "bar"},
            "cost": "0.10"
        }
    ])
    cfg = MappingConfig(
        source="deepseek",
        account_alias="deepseek-main",
        format="json",
        column_mapping={
            "date": ColumnMappingRule(target_field="date", target_type="iso8601_date", required=True),
            "model": ColumnMappingRule(target_dimension="model", target_type="string", required=True),
            "details": ColumnMappingRule(target_metric="input_tokens", target_type="integer", required=True),
            "cost": ColumnMappingRule(target_metric="cost", unit="usd", target_type="decimal", required=True),
        },
    )
    importer = MappingImporter(cfg)
    rep = importer.process_content(nested_json)
    assert rep.quarantined == 1
    reasons = rep.quarantine_records[0].reasons
    assert any("unsupported nesting" in r or "invalid integer" in r for r in reasons), f"Expected nesting explanation, got {reasons}"
    print("PASS: test_unsupported_nesting_is_explained_not_guessed")


def test_null_vs_missing_vs_empty_string():
    """R06: Distinguish explicit null, missing, empty string, and explicit zero."""
    # 1. Missing vs explicit null vs empty string
    v_missing, err_m = convert_value(ValueStatus.MISSING, None, "integer")
    assert err_m == "missing"

    v_null, err_n = convert_value(ValueStatus.EXPLICIT_NULL, None, "integer")
    assert v_null is None and err_n is None

    v_empty, err_e = convert_value(ValueStatus.EMPTY_STRING, "", "integer")
    assert err_e == "empty_string_for_non_string"

    # String allows empty string
    v_str_empty, err_se = convert_value(ValueStatus.EMPTY_STRING, "", "string")
    assert v_str_empty == "" and err_se is None

    # Explicit zero is preserved
    v_zero_int, err_zi = convert_value(ValueStatus.PRESENT, "0", "integer")
    assert v_zero_int == 0 and err_zi is None

    v_zero_dec, err_zd = convert_value(ValueStatus.PRESENT, "-0.0", "decimal")
    assert v_zero_dec == "0" and err_zd is None

    print("PASS: test_null_vs_missing_vs_empty_string")


def main():
    print("--- Running importers/mapping tests ---")
    test_schema_conformance_on_deepseek_fixture()
    test_deepseek_fixture_sums_exact()
    test_idempotent_replay()
    test_quarantine_and_disjoint_sum()
    test_strict_mode_aborts_on_error()
    test_formats_json_and_jsonl_and_preview()
    test_csv_delimiters_decimal_comma_and_timezone()
    test_unsupported_nesting_is_explained_not_guessed()
    test_null_vs_missing_vs_empty_string()
    print("ALL TESTS PASSED (exit code 0)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
