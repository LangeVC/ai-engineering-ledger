from __future__ import annotations

import csv
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TABLES = {
    "dataset": ["measurement_id","observed_at_utc","observed_at_source_offset","source_alias","account_alias","provider","model","input_tokens","output_tokens","cost","currency","confidence"],
    "providers": ["provider","source_alias","coverage_note"],
    "models": ["provider","model","first_seen_utc","last_seen_utc"],
    "costs": ["measurement_id","cost_kind","cost","currency"],
    "git-activity": ["repository_alias","commit_count","period_from","period_to"],
    "releases": ["repository_alias","tag","released_at_utc"],
    "reconciliation": ["subject","result","reason"],
    "provenance": ["measurement_id","evidence_id","evidence_sha256","reproducible"],
}
FORBIDDEN_KEYS = {"secret", "token", "prompt", "chat", "code", "screenshot", "personal_path",
                  "access_token", "api_token", "password", "screenshot_path"}
TRIGGERS = "=+-@\t\r\n '"
SECRET_VALUE = re.compile(
    r"(?i)(bearer\s+\S+|(?:sk[-_]|gh[opurs]_)[A-Za-z0-9_-]{6,}|"
    r"(?:token|secret|password)\s*[=:]\s*\S+)"
)
PRIVATE_HOME_VALUE = re.compile(
    r"(?:^|[\s\"'])(?:~[/\\]|/Users/|/home/|file:///(?:Users|home)/|[A-Za-z]:\\Users\\)"
)
PRIVATE_REMOTE_VALUE = re.compile(
    r"(?i)(?:\bgit@[^\s:]+:[^\s]+|\bssh://\S+|\bhttps?://[^\s]+\.git(?:\b|$)|"
    r"\bhttps?://[^\s/@:]+:[^\s/@]+@|\bhttps?://(?:localhost|127\.0\.0\.1|[^\s/]+\.(?:local|internal))\b)"
)
EMAIL_VALUE = re.compile(r"(?i)(?<![A-Z0-9._%+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![A-Z0-9._%+-])")
ANALYSIS_SCHEMA_VERSION = "0.3"
ANALYSIS_DOMAINS = (
    "resources",
    "runtime",
    "engineering",
    "products",
    "costs",
    "coverage",
    "reconciliation",
    "provenance",
)
DATA_STATES = {"value", "evidenced_zero", "partial", "unknown", "unsupported", "unverified", "unavailable"}


def _guard(value: Any) -> Any:
    text = str(value)
    return "'" + text if text and text[0] in TRIGGERS else text


def _safe_row(row: dict[str, Any], table: str) -> None:
    keys = {k.lower() for k in row}
    if any(key in FORBIDDEN_KEYS or key.endswith(("_secret", "_password", "_prompt", "_chat", "_code"))
           for key in keys):
        raise ValueError(f"forbidden standard-export field in {table}")
    for value in row.values():
        if isinstance(value, str) and _sensitive_text(value):
            raise ValueError(f"sensitive standard-export value in {table}")


def _sensitive_text(value: str) -> bool:
    return bool(
        SECRET_VALUE.search(value)
        or PRIVATE_HOME_VALUE.search(value)
        or PRIVATE_REMOTE_VALUE.search(value)
        or EMAIL_VALUE.search(value)
    )


def _safe_nested(value: Any, domain: str, key: str | None = None) -> None:
    if key is not None:
        lowered = key.lower()
        if lowered in FORBIDDEN_KEYS or lowered.endswith(
            ("_secret", "_password", "_prompt", "_chat", "_code", "_screenshot_path")
        ):
            raise ValueError(f"forbidden analysis-export field in {domain}")
        if _sensitive_text(key):
            raise ValueError(f"sensitive analysis-export key in {domain}")
    if isinstance(value, dict):
        for child_key, child in value.items():
            _safe_nested(child, domain, str(child_key))
    elif isinstance(value, list):
        for child in value:
            _safe_nested(child, domain)
    elif isinstance(value, str) and _sensitive_text(value):
        raise ValueError(f"sensitive analysis-export value in {domain}")


def _canonical_sort(values: list[Any]) -> list[Any]:
    return sorted(
        values,
        key=lambda value: json.dumps(value, sort_keys=True, default=str, separators=(",", ":")),
    )


def _normalise_domain(value: Any, domain: str) -> dict[str, Any]:
    if value is None:
        return {"status": "unavailable", "records": []}
    if isinstance(value, list):
        status = "complete"
        records = value
    elif isinstance(value, dict):
        status = value.get("status")
        records = value.get("records", [])
    else:
        raise ValueError(f"analysis domain {domain} must be a list or status/records object")
    if status not in {"complete", "partial", "unknown", "unsupported", "unverified", "unavailable"}:
        raise ValueError(f"invalid analysis domain status for {domain}")
    if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
        raise ValueError(f"analysis domain records for {domain} must be objects")
    if status in {"unknown", "unsupported", "unverified", "unavailable"} and records:
        raise ValueError(f"analysis domain {domain} cannot carry records while status is {status}")
    clean = []
    for record in records:
        _safe_nested(record, domain)
        state = record.get("state")
        if "value" in record and state is None:
            raise ValueError(f"metric record in {domain} requires an explicit state")
        if state is not None:
            if state not in DATA_STATES:
                raise ValueError(f"invalid data state in {domain}")
            if (
                state in {"unknown", "unsupported", "unverified", "unavailable"}
                and record.get("value") is not None
            ):
                raise ValueError(f"state {state} must not be represented as a numeric value in {domain}")
            if state in {"value", "evidenced_zero", "partial"} and "value" not in record:
                raise ValueError(f"state {state} requires an explicit value in {domain}")
            if state == "evidenced_zero" and (
                isinstance(record["value"], bool)
                or not isinstance(record["value"], (int, float))
                or record["value"] != 0
            ):
                raise ValueError(f"evidenced_zero requires value 0 in {domain}")
        clean.append(record)
    clean.sort(key=lambda record: json.dumps(record, sort_keys=True, default=str, separators=(",", ":")))
    return {"status": status, "records": clean}


def _canonical_hash(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode() + b"\0" + files[name])
    return digest.hexdigest()


class StudyExporter:
    def __init__(self, transformation_version: str = "0.1.0") -> None:
        self.transformation_version = transformation_version

    def write(self, destination: str | os.PathLike[str], rows: dict[str, list[dict[str, Any]]], *,
              filters: dict[str, Any], timezone_name: str, study_cutoff: str, data_state: dict[str, Any],
              source_status: list[dict[str, Any]] | None = None, missing_periods: list[dict[str, Any]] | None = None,
              confidence_distribution: dict[str, int] | None = None, evidence_hashes: list[dict[str, Any]] | None = None,
              export_id: str = "export-local", export_timestamp: str | None = None,
              include_evidence: bool = False) -> dict[str, Any]:
        dest = Path(destination)
        canonical_source_status = _canonical_sort(list(source_status or []))
        canonical_missing_periods = _canonical_sort(list(missing_periods or []))
        canonical_evidence_hashes = _canonical_sort(list(evidence_hashes or []))

        # Validate all caller-controlled fields that can reach an artifact before
        # creating the destination. A rejected export must not leave a partial pack.
        _safe_nested(
            {
                "rows": rows,
                "filters": filters,
                "timezone": timezone_name,
                "study_cutoff": study_cutoff,
                "data_state": data_state,
                "source_status": canonical_source_status,
                "missing_periods": canonical_missing_periods,
                "confidence_distribution": confidence_distribution or {},
                "evidence_hashes": canonical_evidence_hashes,
                "export_id": export_id,
                "export_timestamp": export_timestamp,
            },
            "study_export",
        )
        table_meta, content_files = [], {}
        json_bundle: dict[str, Any] = {}
        for name, columns in TABLES.items():
            input_rows = rows.get(name, [])
            clean_rows = []
            for row in input_rows:
                _safe_row(row, name)
                clean_rows.append({column: row.get(column, "") for column in columns})
            clean_rows.sort(key=lambda r: json.dumps(r, sort_keys=True, default=str))
            json_bundle[name] = clean_rows
            import io
            stream = io.StringIO(newline="")
            writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
            writer.writeheader()
            for row in clean_rows:
                writer.writerow({k: _guard(v) if isinstance(v, str) else v for k, v in row.items()})
            blob = stream.getvalue().encode()
            filename = f"{name}.csv"
            coverage = "complete" if name in rows else "unavailable"
            table_meta.append({"name": name, "file": filename, "rows": len(clean_rows),
                               "sha256": hashlib.sha256(blob).hexdigest(), "coverage": coverage,
                               **({"coverage_note": "category unavailable; schema-only table"} if coverage == "unavailable" else {})})
            content_files[filename] = blob
        json_blob = (json.dumps(json_bundle, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        content_files["dataset.json"] = json_blob

        analysis_bundle = self._analysis_bundle(
            rows,
            filters=filters,
            timezone_name=timezone_name,
            analysis_cutoff=study_cutoff,
            data_state=data_state,
            source_status=canonical_source_status,
            missing_periods=canonical_missing_periods,
            evidence_hashes=canonical_evidence_hashes,
        )
        analysis_blob = (
            json.dumps(analysis_bundle, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode()
        content_files["analysis.json"] = analysis_blob
        analysis_hash = hashlib.sha256(analysis_blob).hexdigest()
        manifest = {
            "schema_version": "0.1", "transformation_version": self.transformation_version,
            "export_id": export_id,
            "export_timestamp": export_timestamp or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "filters": {**filters, "interval": "[from,to)"}, "timezone": timezone_name,
            "study_cutoff": study_cutoff, "data_state": data_state,
            "csv_presentation_rule": "formula-neutralization-prefix-apostrophe@0.1",
            "units": {"tokens": "tokens", "cost": "USD", "time": "ISO-8601 UTC, source offset kept in *_source_offset columns"},
            "tables": table_meta, "source_status": canonical_source_status,
            "missing_periods": canonical_missing_periods,
            "confidence_distribution": confidence_distribution or {},
            "evidence_hashes": canonical_evidence_hashes,
            "content_hash": _canonical_hash(content_files),
        }
        analysis_manifest = {
            "schema_version": ANALYSIS_SCHEMA_VERSION,
            "contract": "ael.study.analysis",
            "transformation_version": self.transformation_version,
            "export_id": export_id,
            "export_timestamp": manifest["export_timestamp"],
            "filters": analysis_bundle["filters"],
            "timezone": timezone_name,
            "analysis_cutoff": study_cutoff,
            "source_status": analysis_bundle["source_status"],
            "source_gaps": analysis_bundle["source_gaps"],
            "transformations": analysis_bundle["transformations"],
            "files": [
                {"name": "analysis.json", "size_bytes": len(analysis_blob), "sha256": analysis_hash}
            ],
            "content_hash": analysis_hash,
        }
        # Scan the final structures as a defense against future derived fields
        # bypassing the caller-input preflight.
        _safe_nested(
            {
                "dataset": json_bundle,
                "analysis": analysis_bundle,
                "manifest": manifest,
                "analysis_manifest": analysis_manifest,
            },
            "study_export",
        )

        dest.mkdir(mode=0o700, parents=True, exist_ok=True)
        for filename, blob in content_files.items():
            (dest / filename).write_bytes(blob)
            os.chmod(dest / filename, 0o600)
        (dest / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        os.chmod(dest / "manifest.json", 0o600)
        (dest / "analysis-manifest.json").write_text(
            json.dumps(analysis_manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        os.chmod(dest / "analysis-manifest.json", 0o600)
        if include_evidence:
            (dest / "EVIDENCE_EXPORT_REQUESTED").write_text("Evidence must be copied by an explicit evidence-store operation.\n")
            os.chmod(dest / "EVIDENCE_EXPORT_REQUESTED", 0o600)
        return manifest

    def _analysis_bundle(
        self,
        rows: dict[str, Any],
        *,
        filters: dict[str, Any],
        timezone_name: str,
        analysis_cutoff: str,
        data_state: dict[str, Any],
        source_status: list[dict[str, Any]],
        missing_periods: list[dict[str, Any]],
        evidence_hashes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        start = filters.get("start", filters.get("from"))
        end_exclusive = filters.get("end_exclusive", filters.get("to"))
        if not start or not end_exclusive:
            raise ValueError("analysis filters require start/from and end_exclusive/to")

        derived_inputs: dict[str, Any] = {
            "resources": rows.get("resources"),
            "runtime": rows.get("runtime", rows.get("dataset")),
            "engineering": rows.get("engineering"),
            "products": rows.get("products"),
            "costs": rows.get("costs"),
            "coverage": rows.get("coverage"),
            "reconciliation": rows.get("reconciliation"),
            "provenance": rows.get("provenance"),
        }
        domains = {name: _normalise_domain(derived_inputs[name], name) for name in ANALYSIS_DOMAINS}
        phase_counts = {"reconstructed": 0, "prospective": 0, "unknown": 0}
        for domain in domains.values():
            for record in domain["records"]:
                phase = record.get("collection_phase")
                if phase in {"historical", "reconstructed"}:
                    phase_counts["reconstructed"] += 1
                elif phase == "prospective":
                    phase_counts["prospective"] += 1
                else:
                    phase_counts["unknown"] += 1

        metadata = {
            "source_status": source_status,
            "source_gaps": missing_periods,
            "evidence_hashes": evidence_hashes,
            "data_state": data_state,
        }
        _safe_nested(metadata, "analysis_metadata")
        return {
            "schema_version": ANALYSIS_SCHEMA_VERSION,
            "contract": "ael.study.analysis",
            "transformation_version": self.transformation_version,
            "filters": {
                "start": start,
                "end_exclusive": end_exclusive,
                "semantics": "[start,end_exclusive)",
                **{
                    key: value
                    for key, value in filters.items()
                    if key not in {"from", "to", "start", "end_exclusive", "interval"}
                },
            },
            "timezone": timezone_name,
            "analysis_cutoff": analysis_cutoff,
            "data_state": data_state,
            "source_status": source_status,
            "source_gaps": missing_periods,
            "transformations": [
                {
                    "id": "study-analysis-bundle",
                    "version": self.transformation_version,
                    "input_contract": "AEL ledger and configured study domains",
                    "output_contract": f"ael.study.analysis@{ANALYSIS_SCHEMA_VERSION}",
                }
            ],
            "observation_phase_counts": phase_counts,
            "evidence_hashes": evidence_hashes,
            "domains": domains,
        }
