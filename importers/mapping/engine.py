"""Mapping engine to transform parsed records into canonical events and measurements.
Enforces strict schema validation, quarantine routing, invariants, and disjoint reporting.
"""

import datetime
from decimal import Decimal
import hashlib
import json
import re
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

from .models import (
    ColumnMappingRule,
    CsvOptions,
    ImportMode,
    ImportReport,
    MappingConfig,
    QuarantineRecord,
    RecordOutcome,
    ValueStatus,
)
from .parser import parse_input_records
from .transformer import convert_value, parse_date_to_window


def _generate_ulid(prefix: str = "") -> str:
    """Generate a valid ULID-like 26-char Crockford base32 string matching ^[0-9A-HJKMNP-TV-Z]{26}$."""
    # Crockford base32 alphabet (excludes I, L, O, U)
    crockford = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    u = uuid.uuid4().int
    chars = []
    for _ in range(26):
        chars.append(crockford[u % 32])
        u //= 32
    return "".join(reversed(chars))


def _derive_idempotency_key(source: str, account: str, native_id: str, window: Optional[Dict[str, Any]]) -> str:
    """Derive canonical idempotency key matching DQ03 and schema/core/semantic.py."""
    if window is None:
        w = [None, None, None]
    else:
        starts = window["starts_at"].replace("Z", "+00:00")
        ends = window["ends_at"].replace("Z", "+00:00")
        dt_start = datetime.datetime.fromisoformat(starts).astimezone(datetime.timezone.utc)
        dt_end = datetime.datetime.fromisoformat(ends).astimezone(datetime.timezone.utc)
        w = [window["type"], dt_start.isoformat(), dt_end.isoformat()]
    data = json.dumps(["ael.idem.v0.1", source, account, native_id] + w, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


class MappingImporter:
    """Generic mapping importer supporting CSV, JSON, and JSONL formats."""

    def __init__(self, config: MappingConfig, existing_keys: Optional[Set[str]] = None):
        self.config = config
        self.existing_keys = set(existing_keys) if existing_keys is not None else set()

    def process_content(
        self,
        content: str,
        mode: ImportMode = ImportMode.FULL,
        evidence_id: Optional[str] = None,
    ) -> ImportReport:
        """Process content stream or string.
        mode:
          FULL: Non-strict ingestion; invalid records go to quarantine; status is complete or partial.
          STRICT: Aborts and rejects all records if any record fails validation.
          PREVIEW: Returns preview records without producing final side-effects.
        """
        if evidence_id is None:
            evidence_id = f"ev_{_generate_ulid()}"

        rows = parse_input_records(content, self.config.format, self.config.csv_options)
        report = ImportReport()
        report.total_records = len(rows)

        outcomes: List[RecordOutcome] = []
        has_error = False

        for row_num, row_data in rows:
            outcome = self._process_single_row(row_num, row_data, evidence_id)
            outcomes.append(outcome)
            if outcome.status == "quarantined":
                has_error = True

        # If strict mode and any row has errors, quarantine everything or fail
        if mode == ImportMode.STRICT and has_error:
            report.accepted = 0
            report.existing = 0
            report.quarantined = report.total_records
            report.corrected = 0
            report.run_status = "failed"
            for out in outcomes:
                reasons = out.reasons if out.reasons else ["strict mode rejected: run contained invalid records"]
                report.quarantine_records.append(
                    QuarantineRecord(
                        row_number=out.row_number,
                        source_record={k: v[1] for k, v in rows[out.row_number - 1][1].items()},
                        reasons=reasons,
                    )
                )
            assert report.verify_disjoint_sum()
            return report

        # Normal aggregation across outcomes
        for out in outcomes:
            if out.status == "accepted":
                report.accepted += 1
                if out.event:
                    report.events.append(out.event)
                report.measurements.extend(out.measurements)
            elif out.status == "existing":
                report.existing += 1
            elif out.status == "quarantined":
                report.quarantined += 1
                report.quarantine_records.append(
                    QuarantineRecord(
                        row_number=out.row_number,
                        source_record={k: v[1] for k, v in rows[out.row_number - 1][1].items()},
                        reasons=out.reasons,
                    )
                )
            elif out.status == "corrected":
                report.corrected += 1
                if out.event:
                    report.events.append(out.event)
                report.measurements.extend(out.measurements)

        if report.quarantined > 0:
            report.run_status = "partial"
        else:
            report.run_status = "complete"

        if mode == ImportMode.PREVIEW:
            for out in outcomes:
                report.preview_records.append({
                    "row_number": out.row_number,
                    "status": out.status,
                    "event_type": out.event.get("event_type") if out.event else None,
                    "measurements_count": len(out.measurements),
                    "reasons": out.reasons,
                })

        assert report.verify_disjoint_sum(), f"Disjoint sum invariant broken: {report}"
        return report

    def _process_single_row(
        self,
        row_num: int,
        row_data: Dict[str, Tuple[ValueStatus, Any]],
        evidence_id: str,
    ) -> RecordOutcome:
        errors: List[str] = []
        converted: Dict[str, Any] = {}
        target_metrics: Dict[str, Tuple[str, Any, str, str]] = {}  # col -> (metric, val, unit, aggregation)
        dimensions: Dict[str, Any] = {}
        event_fields: Dict[str, Any] = {}

        # 1. Check mapping rules
        for col_name, rule_def in self.config.column_mapping.items():
            if isinstance(rule_def, dict):
                rule = ColumnMappingRule(**rule_def)
            else:
                rule = rule_def

            if col_name not in row_data:
                if rule.required and rule.default is None:
                    errors.append(f"required column '{col_name}' missing")
                    continue
                else:
                    status = ValueStatus.MISSING
                    raw_val = rule.default
            else:
                status, raw_val = row_data[col_name]
                if status == ValueStatus.MISSING or (status == ValueStatus.EMPTY_STRING and rule.target_type != "string"):
                    if rule.default is not None:
                        status = ValueStatus.PRESENT
                        raw_val = rule.default

            if rule.required and (status == ValueStatus.MISSING or status == ValueStatus.EXPLICIT_NULL or (status == ValueStatus.EMPTY_STRING and rule.target_type == "string" and not raw_val)):
                errors.append(f"required column '{col_name}' is empty or missing")
                continue

            val, err = convert_value(
                status,
                raw_val,
                rule.target_type,
                decimal_sep=self.config.csv_options.decimal_separator,
                thousands_sep=self.config.csv_options.thousands_separator,
            )
            if err:
                errors.append(f"column '{col_name}' error: {err}")
                continue

            converted[col_name] = val

            # Route to target
            if rule.target_metric:
                if val is not None:
                    target_metrics[col_name] = (
                        rule.target_metric,
                        val,
                        rule.unit.lower() if rule.unit else "token",
                        rule.aggregation,
                    )
            elif rule.target_dimension:
                if val is not None:
                    dimensions[rule.target_dimension] = val
            elif rule.target_field:
                if val is not None:
                    event_fields[rule.target_field] = val

        # Invariant checks
        if not errors and self.config.invariants:
            inv_errors = self._evaluate_invariants(converted, self.config.invariants)
            errors.extend(inv_errors)

        if errors:
            return RecordOutcome(
                row_number=row_num,
                status="quarantined",
                reasons=errors,
            )

        # 2. Extract window / dates
        period_start = event_fields.get("period.start") or event_fields.get("period_start")
        period_end = event_fields.get("period.end") or event_fields.get("period_end")
        date_val = event_fields.get("date") or converted.get("date")

        window = None
        if date_val:
            try:
                p_start, p_end, win = parse_date_to_window(
                    str(date_val),
                    self.config.csv_options.timezone,
                    window_type=self.config.window_type,
                    custom_label=self.config.window_custom_label,
                )
                if not period_start:
                    period_start = p_start
                if not period_end:
                    period_end = p_end
                window = win
            except Exception as e:
                return RecordOutcome(
                    row_number=row_num,
                    status="quarantined",
                    reasons=[f"invalid date/window '{date_val}': {e}"],
                )

        model = dimensions.get("model") or event_fields.get("model") or converted.get("model")

        # 3. Derive Idempotency Key
        # If native id given in event_fields, use it; else derive deterministic key from source + account + window + model
        native_id = str(event_fields.get("id") or f"{model}:{period_start}")
        idem_key = _derive_idempotency_key(
            self.config.source,
            self.config.account_alias,
            native_id,
            window,
        )

        if idem_key in self.existing_keys:
            return RecordOutcome(
                row_number=row_num,
                status="existing",
                reasons=["already recorded in active ledger"],
            )

        # Mark as seen in this run to detect in-file duplicates
        self.existing_keys.add(idem_key)

        # 4. Construct Event Envelope
        event_id = _generate_ulid()
        now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")

        event = {
            "schema_version": "0.1",
            "event_id": event_id,
            "event_type": "usage.interval",
            "source": self.config.source,
            "source_account_id": self.config.account_alias,
            "collector": self.config.collector,
            "mapping_version": self.config.mapping_version,
            "collected_at": now_utc,
            "observed_at": period_start,
            "period_start": period_start,
            "period_end": period_end,
            "collection_phase": "historical",
            "project_id": None,
            "repository_id": None,
            "session_id": None,
            "task_id": None,
            "provenance": {
                "class": self.config.default_provenance_class,
                "method": self.config.default_provenance_method,
                "confidence": self.config.default_confidence,
                "confidence_reason": self.config.default_confidence_reason,
                "validation_status": "semantically_checked",
            },
            "raw_evidence_id": evidence_id,
            "idempotency_key": idem_key,
            "window": window,
            "payload": {k: v for k, v in converted.items() if not isinstance(v, (dict, list))},
        }

        # 5. Construct Measurements
        measurements: List[Dict[str, Any]] = []
        for col_name, (metric, val, unit, agg) in target_metrics.items():
            meas_id = _generate_ulid()
            meas = {
                "schema_version": "0.1",
                "measurement_id": meas_id,
                "event_id": event_id,
                "metric": metric,
                "value": val,
                "unit": unit,
                "aggregation": agg,
                "source": self.config.source,
                "source_account_id": self.config.account_alias,
                "provider": self.config.source,
                "model": model,
                "window": window,
                "dimensions": dimensions,
                "provenance": {
                    "class": self.config.default_provenance_class,
                    "method": self.config.default_provenance_method,
                    "confidence": self.config.default_confidence,
                    "confidence_reason": self.config.default_confidence_reason,
                    "validation_status": "semantically_checked",
                },
                "evidence_ids": [evidence_id],
                "collector_version": self.config.collector_version,
                "mapping_version": self.config.mapping_version,
                "revision": {
                    "revision_id": _generate_ulid(),
                    "revision_no": 1,
                    "supersedes": None,
                    "reason_code": "initial",
                    "reason": "initial historical import",
                    "recorded_at": now_utc,
                    "effective": True,
                    "lineage": {
                        "mapping_version": self.config.mapping_version,
                        "transformation_version": "0.1.0",
                        "input_measurement_ids": [],
                    },
                },
            }
            measurements.append(meas)

        return RecordOutcome(
            row_number=row_num,
            status="accepted",
            event=event,
            measurements=measurements,
        )

    def _evaluate_invariants(self, row: Dict[str, Any], invariants: List[str]) -> List[str]:
        """Safely evaluate arithmetic and comparison invariants against row variables."""
        errors: List[str] = []
        context = {}
        for k, v in row.items():
            if isinstance(v, int):
                context[k] = Decimal(v)
            elif isinstance(v, str):
                try:
                    context[k] = Decimal(v)
                except Exception:
                    context[k] = v
            else:
                context[k] = v

        for inv in invariants:
            # We support expressions of form: "A == B + C", "A >= 0", "A <= B", etc.
            try:
                # Compile and eval in restricted scope
                # Invariants only use math comparison operators
                allowed_chars = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_ +-*/><=!().")
                if not set(inv).issubset(allowed_chars):
                    errors.append(f"disallowed invariant syntax: {inv}")
                    continue
                # Evaluate expression with Decimal types
                result = eval(inv, {"__builtins__": {}}, context)
                if not result:
                    errors.append(f"invariant violation: {inv}")
            except Exception as e:
                errors.append(f"invariant error on '{inv}': {e}")

        return errors
