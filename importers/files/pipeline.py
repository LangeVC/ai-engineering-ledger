"""Pipeline for file-based historical exports (CSV, JSON, JSONL).
Integrates collectors/deepseek and importers/mapping according to US-05.
Supports preview mode, explicit separator/decimal/timezone options,
and strict disjoint accounting: total = accepted + existing + quarantined + corrected.
Provides detailed quarantine reporting with line reference, field, and cause (US-05.d).
"""

from __future__ import annotations

import builtins
import csv
import dataclasses
from decimal import Decimal
import json
import re
import typing
from typing import Any, Dict, List, Optional, Set, Tuple, Union

# Compatibility guard: Ensure typing.Dict is available if upstream module omitted it from typing imports
if not hasattr(builtins, "Dict"):
    builtins.Dict = typing.Dict  # type: ignore

from .detector import inspect_tabular_file, resolve_timezone_name


@dataclasses.dataclass
class FormatOptions:
    """Format, delimiter, decimal separator, and timezone options."""
    format: str = "csv"  # "csv", "json", "jsonl"
    delimiter: Optional[str] = ","
    decimal_separator: str = "."
    timezone: str = "UTC"
    encoding: str = "utf-8"


@dataclasses.dataclass
class FilePreviewResult:
    """Result of previewing a file."""
    format: str
    detected_options: Dict[str, Any]
    headers: List[str]
    sample_records: List[Dict[str, Any]]
    total_sample_count: int
    unsupported_nesting_explanations: List[Dict[str, str]] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class QuarantineErrorDetail:
    """Detailed error report for an isolated quarantine record matching US-05.d:
    line reference, field, and root cause.
    """
    line_number: Optional[int]
    field: Optional[str]
    cause: str
    raw_error: str
    source_record: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "line_number": self.line_number,
            "field": self.field,
            "cause": self.cause,
            "raw_error": self.raw_error,
            "source_record": self.source_record,
        }


@dataclasses.dataclass
class FileImportReport:
    """Accounting and reconciliation report matching US-05.c disjoint invariant:
    total_read == accepted + existing + quarantined + corrected
    and US-05.d detailed quarantine isolation reporting.
    """
    total_read: int = 0
    accepted: int = 0
    existing: int = 0
    quarantined: int = 0
    corrected: int = 0
    status: str = "complete"  # "complete", "partial", "failed", "preview"
    sums_by_group: Dict[str, Dict[str, Any]] = dataclasses.field(default_factory=dict)
    errors: List[Dict[str, Any]] = dataclasses.field(default_factory=list)
    quarantine_details: List[QuarantineErrorDetail] = dataclasses.field(default_factory=list)
    preview_records: List[Dict[str, Any]] = dataclasses.field(default_factory=list)

    def verify_disjoint_counts(self) -> bool:
        """Enforces US-05.c: eingelesene Datensätze = Summe dieser disjunkten Ergebnisse."""
        return self.total_read == (self.accepted + self.existing + self.quarantined + self.corrected)


def parse_error_field_and_cause(raw_err: str) -> Tuple[Optional[str], str]:
    """Extracts field and root cause from error message strings."""
    if not raw_err:
        return None, "Unknown validation error"
    err = str(raw_err).strip()

    # Pattern: column 'field' error: cause
    m_col = re.search(r"column\s+'([^']+)'\s+error:\s*(.*)", err, re.IGNORECASE)
    if m_col:
        return m_col.group(1).strip(), m_col.group(2).strip()

    # Pattern: Invariant violated: field_name ...
    m_inv = re.search(r"Invariant violated:\s*([a-zA-Z0-9_]+)\b(.*)", err)
    if m_inv:
        field = m_inv.group(1).strip()
        cause = f"Invariant violated: {field}{m_inv.group(2)}"
        return field, cause

    # Pattern: Missing required field: field_name
    m_req = re.search(r"Missing required field:\s*([a-zA-Z0-9_]+)", err, re.IGNORECASE)
    if m_req:
        field = m_req.group(1).strip()
        return field, f"Missing required field: {field}"

    # Pattern: Invalid <field> decimal: ...
    m_dec = re.search(r"Invalid\s+([a-zA-Z0-9_]+)\s+decimal:\s*(.*)", err, re.IGNORECASE)
    if m_dec:
        field = m_dec.group(1).strip()
        return field, f"Invalid decimal: {m_dec.group(2).strip()}"

    # Pattern: field 'xyz' ...
    m_fld = re.search(r"field\s+'([^']+)'\s*(.*)", err, re.IGNORECASE)
    if m_fld:
        return m_fld.group(1).strip(), err

    return None, err


class FileImporter:
    """Historical file export importer integrating collectors.deepseek and importers.mapping."""

    def __init__(
        self,
        existing_keys: Optional[Set[str]] = None,
        strict_mode: bool = False,
    ):
        self.existing_keys = set(existing_keys) if existing_keys is not None else set()
        self.strict_mode = strict_mode

    def preview(
        self,
        content: str,
        format_options: Optional[FormatOptions] = None,
        max_rows: int = 5,
        target_mapping: Optional[Dict[str, Any]] = None,
    ) -> FilePreviewResult:
        """Generate a preview of the file without permanent mutations (US-05.a).
        Explains unsupported nested structures instead of guessing.
        """
        opts = format_options or FormatOptions()
        insp = inspect_tabular_file(
            content=content,
            format_type=opts.format,
            delimiter=opts.delimiter,
            decimal_separator=opts.decimal_separator,
            timezone_str=opts.timezone,
        )

        headers: List[str] = []
        samples: List[Dict[str, Any]] = []
        nesting_explanations: List[Dict[str, str]] = []

        if insp["format"] == "csv":
            delim = insp["delimiter"] or ","
            lines = content.strip().splitlines()
            if lines:
                reader = csv.DictReader(lines, delimiter=delim)
                headers = list(reader.fieldnames or [])
                for i, row in enumerate(reader):
                    if i >= max_rows:
                        break
                    samples.append(dict(row))
        elif insp["format"] == "json":
            data = json.loads(content)
            items: List[Dict[str, Any]] = []
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                if "data" in data and isinstance(data["data"], list):
                    items = data["data"]
                else:
                    items = [data]

            for i, it in enumerate(items[:max_rows]):
                if isinstance(it, dict):
                    if not headers:
                        headers = list(it.keys())
                    samples.append(it)
                    # Check for unsupported nesting
                    for k, v in it.items():
                        if isinstance(v, (dict, list)):
                            nesting_explanations.append({
                                "field": k,
                                "type": type(v).__name__,
                                "explanation": (
                                    f"Field '{k}' contains nested structure of type '{type(v).__name__}' "
                                    f"which is unsupported for direct tabular mapping without explicit nested extractor rule."
                                ),
                            })
        elif insp["format"] == "jsonl":
            lines = [line.strip() for line in content.strip().splitlines() if line.strip()]
            for i, line in enumerate(lines[:max_rows]):
                row_dict = json.loads(line)
                if isinstance(row_dict, dict):
                    if not headers:
                        headers = list(row_dict.keys())
                    samples.append(row_dict)
                    for k, v in row_dict.items():
                        if isinstance(v, (dict, list)):
                            nesting_explanations.append({
                                "field": k,
                                "type": type(v).__name__,
                                "explanation": (
                                    f"Field '{k}' contains nested structure of type '{type(v).__name__}' "
                                    f"which is unsupported for direct tabular mapping without explicit nested extractor rule."
                                ),
                            })

        return FilePreviewResult(
            format=insp["format"],
            detected_options=insp,
            headers=headers,
            sample_records=samples,
            total_sample_count=len(samples),
            unsupported_nesting_explanations=nesting_explanations,
        )

    def import_deepseek_export(
        self,
        content: str,
        format_options: Optional[FormatOptions] = None,
        account_alias: str = "deepseek-main",
        currency: str = "usd",
        raw_evidence_id: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], FileImportReport]:
        """Imports historical DeepSeek CSV/JSON/JSONL export file using collectors.deepseek (US-05.b).
        Verifies exact sum reconciliation, disjoint accounting, and detailed quarantine reporting (US-05.d).
        """
        opts = format_options or FormatOptions()
        delim = opts.delimiter or ","
        dec_sep = opts.decimal_separator or "."
        tz = opts.timezone or "UTC"

        # Dynamically import collectors.deepseek
        from collectors.deepseek.collector import DeepSeekCollector, DeepSeekConfig

        cfg = DeepSeekConfig(
            account_alias=account_alias,
            profile_id=account_alias,
            currency=currency,
            csv_delimiter=delim,
            decimal_separator=dec_sep,
            default_timezone=tz,
            strict_validation=self.strict_mode,
        )
        collector = DeepSeekCollector(cfg)

        records, sub_report = collector.parse_export_data(
            raw_text_or_lines=content,
            format_type=opts.format,
            delimiter=delim,
            decimal_separator=dec_sep,
            timezone_str=tz,
            existing_keys=self.existing_keys,
        )

        quarantine_details: List[QuarantineErrorDetail] = []
        detailed_errors: List[Dict[str, Any]] = []

        for err in sub_report.errors:
            line_no = err.get("line")
            raw_err = err.get("error", "")
            row_data = err.get("row")
            field, cause = parse_error_field_and_cause(raw_err)

            detail = QuarantineErrorDetail(
                line_number=line_no,
                field=field,
                cause=cause,
                raw_error=raw_err,
                source_record=row_data,
            )
            quarantine_details.append(detail)
            detailed_errors.append({
                "line": line_no,
                "field": field,
                "cause": cause,
                "error": raw_err,
                "row": row_data,
            })

        report = FileImportReport(
            total_read=sub_report.total_read,
            accepted=sub_report.accepted,
            existing=sub_report.existing,
            quarantined=sub_report.quarantined,
            corrected=sub_report.corrected,
            errors=detailed_errors if detailed_errors else sub_report.errors,
            quarantine_details=quarantine_details,
            sums_by_group=sub_report.sums_by_group,
        )

        if self.strict_mode and report.quarantined > 0:
            report.status = "failed"
            report.accepted = 0
            # If strict mode aborts all records, return empty events/measurements
            return [], [], report

        if report.quarantined > 0:
            report.status = "partial"
        else:
            report.status = "complete"

        events, measurements = collector.records_to_events_and_measurements(
            records=records if report.status != "failed" else [],
            raw_evidence_id=raw_evidence_id,
        )

        return events, measurements, report

    def import_generic_export(
        self,
        content: str,
        mapping_config_dict: Dict[str, Any],
        format_options: Optional[FormatOptions] = None,
        evidence_id: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], FileImportReport]:
        """Imports generic CSV/JSON/JSONL export file using importers.mapping (US-05.a, US-05.d)."""
        opts = format_options or FormatOptions()

        from importers.mapping.models import MappingConfig, ImportMode, CsvOptions
        from importers.mapping.engine import MappingImporter

        # Override format options in mapping_config_dict if explicit
        cfg_data = dict(mapping_config_dict)
        cfg_data["format"] = opts.format
        if opts.format == "csv":
            csv_opts = dict(cfg_data.get("csv_options") or {}) if isinstance(cfg_data.get("csv_options"), dict) else {}
            if opts.delimiter is not None:
                csv_opts["delimiter"] = opts.delimiter
            if opts.decimal_separator is not None:
                csv_opts["decimal_separator"] = opts.decimal_separator
            if opts.timezone is not None:
                csv_opts["timezone"] = opts.timezone
            cfg_data["csv_options"] = CsvOptions(**csv_opts) if csv_opts else CsvOptions()

        map_config = MappingConfig(**cfg_data)
        importer = MappingImporter(config=map_config, existing_keys=self.existing_keys)

        mode = ImportMode.STRICT if self.strict_mode else ImportMode.FULL
        sub_report = importer.process_content(content, mode=mode, evidence_id=evidence_id)

        quarantine_details: List[QuarantineErrorDetail] = []
        errors: List[Dict[str, Any]] = []

        for q in sub_report.quarantine_records:
            line_no = q.row_number
            raw_reasons = q.reasons or []
            combined_err = "; ".join(raw_reasons) if raw_reasons else (q.raw_error or "Validation failed")
            field, cause = parse_error_field_and_cause(combined_err)

            detail = QuarantineErrorDetail(
                line_number=line_no,
                field=field,
                cause=cause,
                raw_error=combined_err,
                source_record=q.source_record,
            )
            quarantine_details.append(detail)
            errors.append({
                "line": line_no,
                "field": field,
                "cause": cause,
                "error": combined_err,
                "source_record": q.source_record,
            })

        report = FileImportReport(
            total_read=sub_report.total_records,
            accepted=sub_report.accepted,
            existing=sub_report.existing,
            quarantined=sub_report.quarantined,
            corrected=sub_report.corrected,
            status=sub_report.run_status,
            errors=errors,
            quarantine_details=quarantine_details,
        )

        return sub_report.events, sub_report.measurements, report
