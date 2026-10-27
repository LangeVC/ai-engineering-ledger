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
SECRET_VALUE = re.compile(r"(?i)(bearer\s+\S+|(?:sk|gh[opurs])_[A-Za-z0-9_-]{6,}|(?:token|secret|password)\s*[=:]\s*\S+)")


def _guard(value: Any) -> Any:
    text = str(value)
    return "'" + text if text and text[0] in TRIGGERS else text


def _safe_row(row: dict[str, Any], table: str) -> None:
    keys = {k.lower() for k in row}
    if any(key in FORBIDDEN_KEYS or key.endswith(("_secret", "_password", "_prompt", "_chat", "_code"))
           for key in keys):
        raise ValueError(f"forbidden standard-export field in {table}")
    for value in row.values():
        if isinstance(value, str) and (SECRET_VALUE.search(value) or value.startswith("/Users/")):
            raise ValueError(f"sensitive standard-export value in {table}")


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
        dest.mkdir(mode=0o700, parents=True, exist_ok=True)
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
            (dest / filename).write_bytes(blob)
            os.chmod(dest / filename, 0o600)
            coverage = "complete" if name in rows else "unavailable"
            table_meta.append({"name": name, "file": filename, "rows": len(clean_rows),
                               "sha256": hashlib.sha256(blob).hexdigest(), "coverage": coverage,
                               **({"coverage_note": "category unavailable; schema-only table"} if coverage == "unavailable" else {})})
            content_files[filename] = blob
        json_blob = (json.dumps(json_bundle, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        (dest / "dataset.json").write_bytes(json_blob)
        os.chmod(dest / "dataset.json", 0o600)
        content_files["dataset.json"] = json_blob
        manifest = {
            "schema_version": "0.1", "transformation_version": self.transformation_version,
            "export_id": export_id,
            "export_timestamp": export_timestamp or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "filters": {**filters, "interval": "[from,to)"}, "timezone": timezone_name,
            "study_cutoff": study_cutoff, "data_state": data_state,
            "csv_presentation_rule": "formula-neutralization-prefix-apostrophe@0.1",
            "units": {"tokens": "tokens", "cost": "USD", "time": "ISO-8601 UTC, source offset kept in *_source_offset columns"},
            "tables": table_meta, "source_status": source_status or [], "missing_periods": missing_periods or [],
            "confidence_distribution": confidence_distribution or {}, "evidence_hashes": evidence_hashes or [],
            "content_hash": _canonical_hash(content_files),
        }
        (dest / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.chmod(dest / "manifest.json", 0o600)
        if include_evidence:
            (dest / "EVIDENCE_EXPORT_REQUESTED").write_text("Evidence must be copied by an explicit evidence-store operation.\n")
            os.chmod(dest / "EVIDENCE_EXPORT_REQUESTED", 0o600)
        return manifest
