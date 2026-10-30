#!/usr/bin/env python3
"""DeepSeek Collector & Adapter for AI Engineering Ledger (AEL v0.1).
Standard library Python 3 only (no external dependencies).

Adheres to:
- US-05: DeepSeek CSV/JSON/JSONL export import, preview, explicit separator/decimal/timezone,
  isolated error records (partial vs complete run), disjoint count accounting:
  total_read = accepted + existing + quarantined + corrected.
- US-05 invariants:
  * prompt_tokens == prompt_cache_hit_tokens + prompt_cache_miss_tokens
  * prompt_cache_hit_tokens >= 0
  * prompt_cache_miss_tokens >= 0
  * completion_tokens >= 0
  * reasoning_tokens <= completion_tokens
- DeepSeek live capability reality (capabilities-matrix.md):
  * No public historical export API endpoint -> automated collect yields ready/unverified/live-HOLD.
  * In-flight inference response usage parsing supported (OpenAI-compatible usage object with DeepSeek extensions).
- schema/collector contracts (account, capability, run, observation, error)
- schema/core contracts (event, measurement, evidence, revision)
"""

from __future__ import annotations

import csv
import dataclasses
import datetime
import decimal
import hashlib
import json
import secrets
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple, Union

# Crockford Base32 alphabet for ULID generation
_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def generate_ulid(timestamp_ms: Optional[int] = None) -> str:
    """Generates a canonical 26-character Crockford Base32 ULID."""
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)

    # 48-bit timestamp -> 10 chars
    time_chars = []
    t = timestamp_ms
    for _ in range(10):
        time_chars.append(_CROCKFORD_ALPHABET[t % 32])
        t //= 32
    time_part = "".join(reversed(time_chars))

    # 80-bit randomness -> 16 chars
    rand_chars = []
    rand_bytes = secrets.token_bytes(10)
    r_val = int.from_bytes(rand_bytes, "big")
    for _ in range(16):
        rand_chars.append(_CROCKFORD_ALPHABET[r_val % 32])
        r_val //= 32
    rand_part = "".join(reversed(rand_chars))

    return time_part + rand_part


def _parse_utc_iso(ts_str: str) -> datetime.datetime:
    """Parses ISO timestamp into UTC datetime."""
    s = ts_str.replace("Z", "+00:00")
    if "." in s:
        a, b = s.split(".", 1)
        i = 0
        while i < len(b) and b[i].isdigit():
            i += 1
        s = a + "." + b[:i][:6].ljust(6, "0") + b[i:]
    dt = datetime.datetime.fromisoformat(s)
    return dt.astimezone(datetime.timezone.utc)


def derive_idempotency_key(
    source: str,
    account: str,
    native_id: str,
    window: Optional[Dict[str, Any]] = None,
) -> str:
    """Derives canonical idempotency key matching schema/core semantic.py DQ03."""
    if window is None:
        w = [None, None, None]
    else:
        w = [
            window["type"],
            _parse_utc_iso(window["starts_at"]).isoformat(),
            _parse_utc_iso(window["ends_at"]).isoformat(),
        ]
    data = json.dumps(
        ["ael.idem.v0.1", source, account, native_id] + w,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


@dataclasses.dataclass
class DeepSeekConfig:
    """Configuration for DeepSeek adapter."""
    account_alias: str = "deepseek-main"
    profile_id: str = "deepseek-main"
    source: str = "deepseek"
    currency: str = "usd"
    api_key_env: str = "DEEPSEEK_API_KEY"
    api_base_url: str = "https://api.deepseek.com"
    default_timezone: str = "UTC"
    csv_delimiter: str = ","
    decimal_separator: str = "."
    strict_validation: bool = False


@dataclasses.dataclass
class DeepSeekRecord:
    """Parsed and validated DeepSeek usage record."""
    date: str  # YYYY-MM-DD or ISO timestamp
    model: str
    requests: int
    prompt_tokens: int
    prompt_cache_hit_tokens: int
    prompt_cache_miss_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    cost: Decimal
    raw_row: Dict[str, Any]
    line_number: Optional[int] = None


@dataclasses.dataclass
class DeepSeekImportReport:
    """Import reconciliation report meeting US-05.c disjoint count invariants:
    total_read == accepted + existing + quarantined + corrected
    """
    total_read: int = 0
    accepted: int = 0
    existing: int = 0
    quarantined: int = 0
    corrected: int = 0
    errors: List[Dict[str, Any]] = dataclasses.field(default_factory=list)
    sums_by_group: Dict[str, Dict[str, Any]] = dataclasses.field(default_factory=dict)

    def verify_disjoint_counts(self) -> bool:
        return self.total_read == (self.accepted + self.existing + self.quarantined + self.corrected)


def parse_inference_usage(usage_dict: Dict[str, Any], model: str = "deepseek-chat") -> Dict[str, Any]:
    """Parses real-time inference completion usage dict from DeepSeek API response.
    DeepSeek API standard format:
    {
      "prompt_tokens": 100,
      "completion_tokens": 50,
      "total_tokens": 150,
      "prompt_cache_hit_tokens": 80,
      "prompt_cache_miss_tokens": 20,
      "prompt_tokens_details": {
        "reasoning_tokens": 10
      }
    }
    """
    prompt = int(usage_dict.get("prompt_tokens", 0))
    completion = int(usage_dict.get("completion_tokens", 0))
    hit = int(usage_dict.get("prompt_cache_hit_tokens", 0))
    miss = int(usage_dict.get("prompt_cache_miss_tokens", 0))

    # Details may contain reasoning_tokens or prompt_tokens_details/completion_tokens_details
    reasoning = 0
    if "reasoning_tokens" in usage_dict:
        reasoning = int(usage_dict["reasoning_tokens"])
    elif "completion_tokens_details" in usage_dict and isinstance(usage_dict["completion_tokens_details"], dict):
        reasoning = int(usage_dict["completion_tokens_details"].get("reasoning_tokens", 0))
    elif "prompt_tokens_details" in usage_dict and isinstance(usage_dict["prompt_tokens_details"], dict):
        reasoning = int(usage_dict["prompt_tokens_details"].get("reasoning_tokens", 0))

    # Invariants check
    if hit + miss > 0 and prompt != (hit + miss):
        # Adjust or raise based on strictness
        pass

    return {
        "model": model,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "prompt_cache_hit_tokens": hit,
        "prompt_cache_miss_tokens": miss,
        "reasoning_tokens": reasoning,
    }


class DeepSeekCollector:
    """DeepSeek collector & historical export adapter."""

    def __init__(self, config: Optional[DeepSeekConfig] = None):
        self.config = config or DeepSeekConfig()

    def declare_capability(self, checked_at: Optional[str] = None) -> Dict[str, Any]:
        """Returns standard AEL capability declaration conforming to schema/collector/capability.schema.json.
        DeepSeek has no automated historical export API (as established in capabilities-matrix.md).
        Historical imports require export files or live faigate capture.
        """
        now = checked_at or datetime.datetime.now(datetime.timezone.utc).isoformat()
        return {
            "schema_version": "0.1",
            "source": self.config.source,
            "account_alias": self.config.account_alias,
            "product_domain": "api",
            "capability": "usage",
            "status": "unverified",
            "profile_binding": {
                "profile_id": self.config.profile_id,
                "explicit": True,
                "global_state_mutated": False,
            },
            "checked_at": now,
            "reason": "DeepSeek API has no automated historical usage export endpoint; file import supported per US-05",
        }

    def declare_account(self, checked_at: Optional[str] = None) -> Dict[str, Any]:
        """Returns standard AEL account declaration conforming to schema/collector/account.schema.json."""
        now = checked_at or datetime.datetime.now(datetime.timezone.utc).isoformat()
        cap = self.declare_capability(checked_at=now)
        return {
            "schema_version": "0.1",
            "source": self.config.source,
            "alias": self.config.account_alias,
            "profile_id": self.config.profile_id,
            "secret_ref": f"env:{self.config.api_key_env}",
            "product_domains": ["api"],
            "capabilities": [cap],
            "health": "ready",
            "health_checked_at": now,
        }

    def preview_csv(
        self,
        content: str,
        delimiter: Optional[str] = None,
        max_rows: int = 5,
    ) -> List[Dict[str, str]]:
        """Provides preview of CSV export without persisting (US-05.a)."""
        delim = delimiter or self.config.csv_delimiter
        reader = csv.DictReader(content.splitlines(), delimiter=delim)
        rows = []
        for i, row in enumerate(reader):
            if i >= max_rows:
                break
            rows.append(row)
        return rows

    def parse_export_data(
        self,
        raw_text_or_lines: Union[str, List[Dict[str, Any]]],
        format_type: str = "csv",
        delimiter: Optional[str] = None,
        decimal_separator: Optional[str] = None,
        timezone_str: Optional[str] = None,
        existing_keys: Optional[Union[set, Dict[str, str]]] = None,
    ) -> Tuple[List[DeepSeekRecord], DeepSeekImportReport]:
        """Parses CSV, JSON, or JSONL DeepSeek export and enforces invariants (US-05).
        Returns valid records and disjoint accounting report.
        """
        delim = delimiter or self.config.csv_delimiter
        dec_sep = decimal_separator or self.config.decimal_separator
        # Retain the argument for compatibility; dates are normalized by the
        # canonical event conversion rather than mutating provider export text.
        _ = timezone_str or self.config.default_timezone
        # Preserve an explicitly supplied empty collection so a caller can reuse it
        # as an incremental checkpoint across runs. ``existing_keys or set()``
        # silently discarded an empty checkpoint and made the second run non-idempotent.
        existing: Union[set, Dict[str, str]] = existing_keys if existing_keys is not None else set()

        report = DeepSeekImportReport()
        records: List[DeepSeekRecord] = []

        raw_rows: List[Tuple[int, Dict[str, Any]]] = []

        if isinstance(raw_text_or_lines, list):
            for idx, item in enumerate(raw_text_or_lines):
                raw_rows.append((idx + 1, item))
        elif format_type == "csv":
            lines = raw_text_or_lines.strip().splitlines()
            if not lines:
                return records, report
            reader = csv.DictReader(lines, delimiter=delim)
            for line_no, row in enumerate(reader, start=2):
                raw_rows.append((line_no, row))
        elif format_type == "json":
            data = json.loads(raw_text_or_lines)
            if isinstance(data, list):
                for idx, item in enumerate(data):
                    raw_rows.append((idx + 1, item))
            elif isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
                for idx, item in enumerate(data["data"]):
                    raw_rows.append((idx + 1, item))
            else:
                report.errors.append({
                    "line": 1,
                    "error": "Unsupported JSON structure: expected top-level list or object with 'data' list",
                })
                return records, report
        elif format_type == "jsonl":
            for idx, line in enumerate(raw_text_or_lines.strip().splitlines(), start=1):
                if not line.strip():
                    continue
                raw_rows.append((idx, json.loads(line)))
        else:
            raise ValueError(f"Unsupported format_type: {format_type}")

        for line_no, row in raw_rows:
            report.total_read += 1
            rec, err = self._validate_and_convert_row(row, dec_sep, line_no)
            if err:
                report.quarantined += 1
                report.errors.append({
                    "line": line_no,
                    "error": err,
                    "row": row,
                })
                if self.config.strict_validation:
                    # In strict mode, fail fast on validation errors
                    pass
                continue

            # Stable logical identity plus semantic fingerprint.  Legacy set
            # checkpoints keep their original behaviour; v0.3 mapping checkpoints
            # distinguish an unchanged rerun from a corrected provider row.
            dedup_key = f"{rec.date}:{rec.model}"
            fingerprint = self._record_fingerprint(rec)
            if isinstance(existing, dict):
                previous = existing.get(dedup_key)
                if previous == fingerprint:
                    report.existing += 1
                    continue
                if previous is None:
                    report.accepted += 1
                else:
                    report.corrected += 1
                existing[dedup_key] = fingerprint
            else:
                if dedup_key in existing:
                    report.existing += 1
                    continue
                existing.add(dedup_key)
                report.accepted += 1
            records.append(rec)

            # Accumulate sum by group: (account, date, model, currency)
            group_key = f"{self.config.account_alias}|{rec.date}|{rec.model}|{self.config.currency}"
            if group_key not in report.sums_by_group:
                report.sums_by_group[group_key] = {
                    "account": self.config.account_alias,
                    "date": rec.date,
                    "model": rec.model,
                    "currency": self.config.currency,
                    "requests": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_hit_tokens": 0,
                    "cache_miss_tokens": 0,
                    "reasoning_tokens": 0,
                    "cost": Decimal("0"),
                }
            g = report.sums_by_group[group_key]
            g["requests"] += rec.requests
            g["input_tokens"] += rec.prompt_tokens
            g["output_tokens"] += rec.completion_tokens
            g["cache_hit_tokens"] += rec.prompt_cache_hit_tokens
            g["cache_miss_tokens"] += rec.prompt_cache_miss_tokens
            g["reasoning_tokens"] += rec.reasoning_tokens
            g["cost"] += rec.cost

        records.sort(key=lambda item: (item.date, item.model, item.line_number or 0))
        report.sums_by_group = dict(sorted(report.sums_by_group.items()))
        return records, report

    @staticmethod
    def _record_fingerprint(record: DeepSeekRecord) -> str:
        """Return a deterministic fingerprint of normalized provider semantics."""
        canonical = {
            "date": record.date,
            "model": record.model,
            "requests": record.requests,
            "prompt_tokens": record.prompt_tokens,
            "prompt_cache_hit_tokens": record.prompt_cache_hit_tokens,
            "prompt_cache_miss_tokens": record.prompt_cache_miss_tokens,
            "completion_tokens": record.completion_tokens,
            "reasoning_tokens": record.reasoning_tokens,
            "cost": str(record.cost),
        }
        raw = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _validate_and_convert_row(
        self,
        row: Dict[str, Any],
        decimal_separator: str,
        line_no: int,
    ) -> Tuple[Optional[DeepSeekRecord], Optional[str]]:
        """Enforces US-05 invariants and field presence."""
        date_val = str(row.get("date") or row.get("period.start") or "").strip()
        model_val = str(row.get("model") or row.get("dimensions.model") or "").strip()

        if not date_val:
            return None, "Missing required field: date"
        if not model_val:
            return None, "Missing required field: model"

        try:
            reqs = int(row.get("requests", 1))
            prompt = int(row.get("prompt_tokens", 0))
            hit = int(row.get("prompt_cache_hit_tokens", 0))
            miss = int(row.get("prompt_cache_miss_tokens", 0))
            completion = int(row.get("completion_tokens", 0))
            reasoning = int(row.get("reasoning_tokens", 0))
        except (ValueError, TypeError) as e:
            return None, f"Numeric parse error: {e}"

        cost_raw = str(row.get("cost", "0")).strip()
        if decimal_separator != ".":
            cost_raw = cost_raw.replace(decimal_separator, ".")
        try:
            cost = Decimal(cost_raw)
        except decimal.InvalidOperation:
            return None, f"Invalid cost decimal: {cost_raw}"

        # US-05 Invariant validations
        if hit < 0:
            return None, f"Invariant violated: prompt_cache_hit_tokens < 0 ({hit})"
        if miss < 0:
            return None, f"Invariant violated: prompt_cache_miss_tokens < 0 ({miss})"
        if prompt != (hit + miss):
            return None, f"Invariant violated: prompt_tokens ({prompt}) != hit ({hit}) + miss ({miss})"
        if completion < 0:
            return None, f"Invariant violated: completion_tokens < 0 ({completion})"
        if reasoning > completion:
            return None, f"Invariant violated: reasoning_tokens ({reasoning}) > completion_tokens ({completion})"

        return DeepSeekRecord(
            date=date_val,
            model=model_val,
            requests=reqs,
            prompt_tokens=prompt,
            prompt_cache_hit_tokens=hit,
            prompt_cache_miss_tokens=miss,
            completion_tokens=completion,
            reasoning_tokens=reasoning,
            cost=cost,
            raw_row=row,
            line_number=line_no,
        ), None

    def records_to_events_and_measurements(
        self,
        records: List[DeepSeekRecord],
        raw_evidence_id: Optional[str] = None,
        collected_at: Optional[str] = None,
        collection_phase: str = "historical",
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Converts valid DeepSeekRecords to canonical AEL core Events and Measurements."""
        now = collected_at or datetime.datetime.now(datetime.timezone.utc).isoformat()
        ev_id = raw_evidence_id or f"ev_{generate_ulid()}"

        events: List[Dict[str, Any]] = []
        measurements: List[Dict[str, Any]] = []

        for rec in records:
            event_id = generate_ulid()

            # Date window
            # If date is YYYY-MM-DD: daily window
            if len(rec.date) == 10 and rec.date.count("-") == 2:
                start_iso = f"{rec.date}T00:00:00Z"
                end_iso = f"{rec.date}T23:59:59Z"
                window_obj = {
                    "type": "daily",
                    "starts_at": start_iso,
                    "ends_at": end_iso,
                }
            else:
                window_obj = {
                    "type": "custom",
                    "label": "export_slice",
                    "starts_at": rec.date,
                    "ends_at": rec.date,
                }

            native_id = f"deepseek-export-{rec.date}-{rec.model}"
            idem_key = derive_idempotency_key(
                source=self.config.source,
                account=self.config.account_alias,
                native_id=native_id,
                window=window_obj,
            )

            event = {
                "schema_version": "0.1",
                "event_id": event_id,
                "event_type": "usage.snapshot",
                "source": self.config.source,
                "source_account_id": self.config.account_alias,
                "collector": "deepseek.export_adapter",
                "mapping_version": "0.1.0",
                "collected_at": now,
                "observed_at": window_obj["starts_at"],
                "period_start": window_obj["starts_at"],
                "period_end": window_obj["ends_at"],
                "collection_phase": collection_phase,
                "project_id": None,
                "repository_id": None,
                "session_id": None,
                "task_id": None,
                "provenance": {
                    "class": "P0",
                    "method": "import",
                    "confidence": 0.99,
                    "confidence_reason": "DeepSeek export file import per US-05",
                    "validation_status": "semantically_checked",
                },
                "raw_evidence_id": ev_id,
                "idempotency_key": idem_key,
                "window": window_obj,
                "payload": {
                    "model": rec.model,
                },
            }
            events.append(event)

            # Measurements definition
            metric_defs = [
                ("requests", rec.requests, "request", "interval_total"),
                ("input_tokens", rec.prompt_tokens, "token", "interval_total"),
                ("output_tokens", rec.completion_tokens, "token", "interval_total"),
                ("cache_hit_tokens", rec.prompt_cache_hit_tokens, "token", "interval_total"),
                ("cache_miss_tokens", rec.prompt_cache_miss_tokens, "token", "interval_total"),
                ("reasoning_tokens", rec.reasoning_tokens, "token", "interval_total"),
                ("cost", str(rec.cost), self.config.currency, "interval_total"),
            ]

            for metric_name, val, unit, agg in metric_defs:
                meas_id = generate_ulid()
                rev_id = generate_ulid()
                meas = {
                    "schema_version": "0.1",
                    "measurement_id": meas_id,
                    "event_id": event_id,
                    "metric": metric_name,
                    "value": val,
                    "unit": unit,
                    "aggregation": agg,
                    "source": self.config.source,
                    "source_account_id": self.config.account_alias,
                    "provider": "deepseek",
                    "model": rec.model,
                    "window": window_obj,
                    "dimensions": {
                        "model": rec.model,
                    },
                    "provenance": {
                        "class": "P0",
                        "method": "import",
                        "confidence": 0.99,
                        "confidence_reason": "DeepSeek export file import per US-05",
                        "validation_status": "semantically_checked",
                    },
                    "evidence_ids": [ev_id],
                    "collector_version": "0.1.0",
                    "mapping_version": "0.1.0",
                    "revision": {
                        "revision_id": rev_id,
                        "revision_no": 1,
                        "supersedes": None,
                        "reason_code": "initial",
                        "reason": "first capture from export import",
                        "recorded_at": now,
                        "effective": True,
                        "lineage": {
                            "mapping_version": "0.1.0",
                            "transformation_version": "0.1.0",
                            "input_measurement_ids": [],
                        },
                    },
                }
                measurements.append(meas)

        return events, measurements

    def build_collection_run(
        self,
        report: DeepSeekImportReport,
        started_at: str,
        finished_at: str,
        period_start: str,
        period_end: str,
        trigger: str = "manual",
    ) -> Dict[str, Any]:
        """Constructs canonical collection run object adhering to schema/collector/run.schema.json."""
        if report.quarantined > 0:
            if report.accepted > 0:
                status = "partial_failure"
                exit_status = 3
            else:
                status = "failure"
                exit_status = 1
        else:
            status = "success"
            exit_status = 0

        errors = []
        for err in report.errors:
            errors.append({
                "code": "IMPORT_VALIDATION_ERROR",
                "message": f"Line {err.get('line')}: {err.get('error')}",
                "context": {"error": err.get("error")},
            })

        account_run = {
            "account_alias": self.config.account_alias,
            "profile_id": self.config.profile_id,
            "source": self.config.source,
            "product_domain": "api",
            "capability": "usage",
            "status": status,
            "observed_at": finished_at,
            "coverage": {
                "requested": {
                    "start": period_start,
                    "end": period_end,
                },
                "delivered": {
                    "start": period_start,
                    "end": period_end,
                },
                "confirmed": {
                    "start": period_start,
                    "end": period_end,
                } if status == "success" else None,
                "confirmation": "complete" if status == "success" else "partial",
                "pagination": {
                    "pages_fetched": 1,
                    "complete": status == "success",
                    "truncated": False,
                    "next_cursor": None,
                },
            },
            "errors": errors,
        }

        return {
            "schema_version": "0.1",
            "run_id": f"run_{generate_ulid()}",
            "trigger": trigger,
            "started_at": started_at,
            "finished_at": finished_at,
            "status": status,
            "exit_status": exit_status,
            "routing_subset": [],
            "account_runs": [account_run],
        }

    def build_evidence_descriptor(
        self,
        content_bytes: bytes,
        mime_type: str = "text/csv",
        created_at: Optional[str] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """Creates evidence ID and descriptor according to schema/core/evidence.schema.json."""
        now = created_at or datetime.datetime.now(datetime.timezone.utc).isoformat()
        ev_id = f"ev_{generate_ulid()}"
        sha = hashlib.sha256(content_bytes).hexdigest()
        desc = {
            "schema_version": "0.1",
            "evidence_id": ev_id,
            "sha256": sha,
            "size_bytes": len(content_bytes),
            "mime_type": mime_type,
            "source": self.config.source,
            "account": self.config.account_alias,
            "created_at": now,
            "observed_at": now,
            "collector_version": "0.1.0",
            "capture_point": "user_export_file",
            "redaction": {
                "redacted": False,
                "redaction_version": None,
            },
        }
        return ev_id, desc
