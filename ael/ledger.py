"""Operational SQLite ingestion and reporting for local AEL usage.

This module intentionally consumes captured files.  It does not authenticate to
providers and therefore never stores credentials or claims live access.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from collectors.byteplus.collector import BytePlusCollector
from collectors.deepseek.collector import DeepSeekCollector, DeepSeekConfig, parse_inference_usage
from collectors.kilo.adapter import KiloAdapter
from ael.study_view import build_ledger_dashboard_view
from importers.manual.importer import export_canonical_measurements, parse_manual_template_json


SCHEMA_VERSION = "0.2"
GAUGE_METRICS = {
    "quota_limit", "quota_used", "quota_remaining", "quota_percent_used",
    "quota_percent_remaining", "credit_balance", "credits_consumed",
    "total_balance", "granted_balance", "topped_up_balance", "available_balance",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_timestamp(value: Any, fallback: str | None = None) -> str:
    raw = str(value or fallback or utc_now()).strip()
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        raw += "T00:00:00Z"
    if raw.endswith("Z"):
        candidate = raw[:-1] + "+00:00"
    else:
        candidate = raw
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(f"timestamp must be RFC3339/date, got {raw!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include timezone, got {raw!r}")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def exact_number(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        raise ValueError("measurement value must be numeric")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"measurement value must be numeric, got {value!r}") from exc
    if not number.is_finite():
        raise ValueError("measurement value must be finite")
    return format(number, "f")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _observation(
    *, source: str, account: str, metric: str, value: Any, unit: str,
    observed_at: Any, evidence_sha256: str, aggregation: str | None = None,
    provider: str | None = None, model: str | None = None,
    scope: str = "provider", dimensions: dict[str, Any] | None = None,
    window_type: str | None = None, window_label: str | None = None,
    reset_at: str | None = None, period_start: str | None = None,
    period_end: str | None = None, provenance_method: str = "file_import",
    provenance_class: str = "P0", confidence: Any = "1",
) -> dict[str, Any]:
    if not source or not account or not metric or not unit:
        raise ValueError("source, account, metric and unit are required")
    observed = normalize_timestamp(observed_at)
    reset = normalize_timestamp(reset_at) if reset_at else None
    start = normalize_timestamp(period_start) if period_start else None
    end = normalize_timestamp(period_end) if period_end else None
    confidence_text = exact_number(confidence)
    if Decimal(confidence_text) < 0 or Decimal(confidence_text) > 1:
        raise ValueError("confidence must be between 0 and 1")
    aggregation = aggregation or ("gauge" if metric in GAUGE_METRICS else "interval_total")
    dims = dimensions or {}
    identity = {
        "source": source, "account": account, "metric": metric,
        "value": exact_number(value), "unit": unit, "aggregation": aggregation,
        "observed_at": observed, "scope": scope, "provider": provider,
        "model": model, "window_type": window_type, "window_label": window_label,
        "reset_at": reset, "period_start": start, "period_end": end,
        "dimensions": dims, "evidence_sha256": evidence_sha256,
    }
    return {
        **identity,
        "idempotency_key": hashlib.sha256(_canonical_json(identity).encode()).hexdigest(),
        "provenance_class": provenance_class,
        "provenance_method": provenance_method,
        "confidence": confidence_text,
        "dimensions_json": _canonical_json(dims),
    }


class OperationalLedger:
    def __init__(self, database: str | Path):
        self.database = Path(database)

    def initialize(self) -> None:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database) as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS metadata (
                  key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS import_receipts (
                  receipt_id TEXT PRIMARY KEY,
                  source TEXT NOT NULL,
                  account TEXT NOT NULL,
                  kind TEXT NOT NULL,
                  evidence_sha256 TEXT NOT NULL,
                  evidence_name TEXT NOT NULL,
                  imported_at TEXT NOT NULL,
                  accepted INTEGER NOT NULL,
                  existing_count INTEGER NOT NULL,
                  rejected INTEGER NOT NULL,
                  incomplete INTEGER NOT NULL,
                  errors_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS observations (
                  observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  idempotency_key TEXT NOT NULL UNIQUE,
                  source TEXT NOT NULL,
                  account TEXT NOT NULL,
                  provider TEXT,
                  model TEXT,
                  metric TEXT NOT NULL,
                  value_text TEXT NOT NULL,
                  unit TEXT NOT NULL,
                  aggregation TEXT NOT NULL,
                  scope TEXT NOT NULL,
                  observed_at TEXT NOT NULL,
                  period_start TEXT,
                  period_end TEXT,
                  window_type TEXT,
                  window_label TEXT,
                  reset_at TEXT,
                  provenance_class TEXT NOT NULL,
                  provenance_method TEXT NOT NULL,
                  confidence TEXT NOT NULL,
                  evidence_sha256 TEXT NOT NULL,
                  evidence_name TEXT NOT NULL,
                  dimensions_json TEXT NOT NULL,
                  imported_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_observations_scope
                  ON observations(source, account, observed_at);
                """
            )
            conn.execute("INSERT OR REPLACE INTO metadata VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
        os.chmod(self.database, 0o600)

    def health(self) -> dict[str, Any]:
        if not self.database.is_file():
            return {"database_exists": False, "schema_version": None, "operational_tables": False}
        with sqlite3.connect(self.database) as conn:
            names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            row = conn.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone() if "metadata" in names else None
        return {
            "database_exists": True,
            "schema_version": row[0] if row else None,
            "operational_tables": {"observations", "import_receipts"}.issubset(names),
        }

    def ingest_file(self, *, source: str, account: str, kind: str, input_path: str | Path,
                    observed_at: str | None = None,
                    evidence_sha256_override: str | None = None) -> dict[str, Any]:
        path = Path(input_path)
        raw = path.read_bytes()
        evidence_sha = evidence_sha256_override or hashlib.sha256(raw).hexdigest()
        if len(evidence_sha) != 64 or any(char not in "0123456789abcdef" for char in evidence_sha):
            raise ValueError("evidence SHA-256 override must be 64 lowercase hexadecimal characters")
        evidence_name = path.name
        try:
            observations, rejected, errors, incomplete = normalize_capture(
                source=source, account=account, kind=kind, raw=raw,
                evidence_sha256=evidence_sha, observed_at=observed_at,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, csv.Error, ValueError) as exc:
            observations, rejected, errors, incomplete = [], 1, [str(exc)], False

        self.initialize()
        accepted = 0
        existing = 0
        imported_at = utc_now()
        receipt_id = "rcpt_" + hashlib.sha256(
            f"{source}\0{account}\0{kind}\0{evidence_sha}\0{imported_at}".encode()
        ).hexdigest()[:24]
        with sqlite3.connect(self.database) as conn:
            for item in observations:
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO observations (
                      idempotency_key, source, account, provider, model, metric,
                      value_text, unit, aggregation, scope, observed_at,
                      period_start, period_end, window_type, window_label, reset_at,
                      provenance_class, provenance_method, confidence,
                      evidence_sha256, evidence_name, dimensions_json, imported_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        item["idempotency_key"], item["source"], item["account"],
                        item.get("provider"), item.get("model"), item["metric"],
                        item["value"], item["unit"], item["aggregation"], item["scope"],
                        item["observed_at"], item.get("period_start"), item.get("period_end"),
                        item.get("window_type"), item.get("window_label"), item.get("reset_at"),
                        item["provenance_class"], item["provenance_method"], item["confidence"],
                        evidence_sha, evidence_name, item["dimensions_json"], imported_at,
                    ),
                )
                if cursor.rowcount == 1:
                    accepted += 1
                else:
                    existing += 1
            conn.execute(
                """INSERT INTO import_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (receipt_id, source, account, kind, evidence_sha, evidence_name, imported_at,
                 accepted, existing, rejected, 1 if incomplete else 0, _canonical_json(errors)),
            )
        return {
            "receipt_id": receipt_id, "source": source, "account": account, "kind": kind,
            "evidence_sha256": evidence_sha, "evidence_name": evidence_name,
            "accepted": accepted, "existing": existing, "rejected": rejected,
            "incomplete": bool(incomplete), "errors": errors,
        }

    def provider_report(
        self,
        from_time: str | None = None,
        to_time: str | None = None,
        *,
        end_exclusive: str | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        if to_time and end_exclusive:
            raise ValueError("use either to_time or end_exclusive, not both")
        clauses: list[str] = []
        params: list[str] = []
        if from_time:
            clauses.append("observed_at >= ?")
            params.append(normalize_timestamp(from_time))
        if to_time:
            clauses.append("observed_at <= ?")
            params.append(normalize_timestamp(to_time))
        if end_exclusive:
            clauses.append("observed_at < ?")
            params.append(normalize_timestamp(end_exclusive))
        query = "SELECT * FROM observations"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY observed_at, observation_id"
        with sqlite3.connect(self.database) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()

        groups: dict[tuple[str, ...], dict[str, Any]] = {}
        for row in rows:
            key = (
                row["source"], row["account"], row["provider"] or "",
                row["model"] or "", row["metric"], row["unit"],
                row["scope"], row["aggregation"], row["window_type"] or "",
                row["window_label"] or "",
            )
            group = groups.setdefault(key, {
                "source": key[0], "account": key[1], "provider": key[2] or None,
                "model": key[3] or None, "metric": key[4], "unit": key[5],
                "scope": key[6], "aggregation": key[7], "window_type": key[8] or None,
                "window_label": key[9] or None, "latest_dimensions": json.loads(row["dimensions_json"]),
                "latest_reset_at": row["reset_at"],
                "additive_across_scopes": key[0] != "kilo",
                "observation_count": 0,
                "first_observed_at": row["observed_at"], "last_observed_at": row["observed_at"],
                "confidence_distribution": {}, "value": "0",
                "provenance_distribution": {}, "evidence_ids": set(),
            })
            group["observation_count"] += 1
            group["last_observed_at"] = row["observed_at"]
            group["latest_dimensions"] = json.loads(row["dimensions_json"])
            group["latest_reset_at"] = row["reset_at"]
            confidence = row["confidence"]
            group["confidence_distribution"][confidence] = group["confidence_distribution"].get(confidence, 0) + 1
            provenance = f"{row['provenance_class']}:{row['provenance_method']}"
            group["provenance_distribution"][provenance] = group["provenance_distribution"].get(provenance, 0) + 1
            group["evidence_ids"].add(row["evidence_sha256"])
            if row["aggregation"] == "gauge":
                group["value"] = row["value_text"]
            else:
                group["value"] = format(Decimal(group["value"]) + Decimal(row["value_text"]), "f")
        rendered_groups = []
        for key in sorted(groups):
            group = groups[key]
            group["evidence_ids"] = sorted(group["evidence_ids"])
            rendered_groups.append(group)
        return {
            "schema_version": SCHEMA_VERSION,
            "filters": {"from": normalize_timestamp(from_time) if from_time else None,
                        "to": normalize_timestamp(to_time) if to_time else None,
                        "end_exclusive": normalize_timestamp(end_exclusive) if end_exclusive else None},
            "observation_count": len(rows),
            "groups": rendered_groups,
        }

    def study_report(self, start: str | None = None, end_exclusive: str | None = None) -> dict[str, Any]:
        """Return the canonical v0.3 study view for the local ledger."""
        self.initialize()
        return build_ledger_dashboard_view(
            self.database,
            start=start,
            end_exclusive=end_exclusive,
            timezone_name="Europe/Berlin",
        )


def _finalize(item: dict[str, Any]) -> dict[str, Any]:
    item = dict(item)
    item["value"] = exact_number(item["value"])
    return item


def _from_canonical(row: dict[str, Any], *, source: str, account: str,
                    evidence_sha: str, fallback_time: str | None) -> dict[str, Any]:
    dimensions = row.get("dimensions") or {}
    window = row.get("window") or {}
    revision = row.get("revision") or {}
    provenance = row.get("provenance") or {}
    observed = row.get("observed_at") or row.get("date") or revision.get("recorded_at") or revision.get("created_at") or fallback_time
    return _observation(
        source=row.get("source") or source,
        account=account,
        metric=row["metric"], value=row["value"], unit=row["unit"],
        aggregation=row.get("aggregation"), observed_at=observed,
        provider=row.get("provider"), model=row.get("model"),
        scope=dimensions.get("scope_level", row.get("scope", "provider")),
        dimensions=dimensions, window_type=window.get("type") or dimensions.get("window_type"),
        window_label=window.get("label") or dimensions.get("window_label"),
        reset_at=window.get("reset_at") or dimensions.get("reset_at"),
        period_start=window.get("starts_at") or row.get("period_start"),
        period_end=window.get("ends_at") or row.get("period_end"),
        provenance_method=provenance.get("method", "file_import"),
        provenance_class=provenance.get("class", "P0"),
        confidence=provenance.get("confidence", "1"), evidence_sha256=evidence_sha,
    )


def _numeric_leaves(data: Any, prefix: str = "") -> Iterable[tuple[str, Any]]:
    if isinstance(data, dict):
        for key, value in data.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, (dict, list)):
                yield from _numeric_leaves(value, path)
            elif isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
                yield path, value
            elif isinstance(value, str):
                try:
                    Decimal(value)
                except InvalidOperation:
                    continue
                yield path, value
    elif isinstance(data, list):
        for index, value in enumerate(data):
            yield from _numeric_leaves(value, f"{prefix}[{index}]")


def _byteplus_metric_allowed(field_path: str) -> bool:
    leaf = field_path.rsplit(".", 1)[-1].lower()
    include = (
        "token", "request", "quota", "usage", "amount", "cost", "price",
        "settlement", "balance", "credit", "discount", "count",
    )
    exclude = (
        "page", "offset", "limit", "status", "code", "timestamp", "time",
        "year", "month", "day", "id", "index", "total_pages",
    )
    return any(word in leaf for word in include) and not any(word in leaf for word in exclude)


def normalize_capture(*, source: str, account: str, kind: str, raw: bytes,
                      evidence_sha256: str, observed_at: str | None) -> tuple[list[dict[str, Any]], int, list[str], bool]:
    text = raw.decode("utf-8")
    fallback = normalize_timestamp(observed_at) if observed_at else utc_now()
    rejected = 0
    errors: list[str] = []
    incomplete = False
    output: list[dict[str, Any]] = []

    if kind == "deepseek-csv":
        collector = DeepSeekCollector(DeepSeekConfig(account_alias=account))
        records, report = collector.parse_export_data(text, format_type="csv")
        rejected = report.quarantined
        errors = [str(item.get("error")) for item in report.errors]
        for record in records:
            values = {
                "requests": (record.requests, "request"),
                "input_tokens": (record.prompt_tokens, "token"),
                "cache_hit_tokens": (record.prompt_cache_hit_tokens, "token"),
                "cache_miss_tokens": (record.prompt_cache_miss_tokens, "token"),
                "output_tokens": (record.completion_tokens, "token"),
                "reasoning_tokens": (record.reasoning_tokens, "token"),
                "cost": (record.cost, collector.config.currency),
            }
            for metric, (value, unit) in values.items():
                output.append(_observation(source="deepseek", account=account, metric=metric,
                    value=value, unit=unit, aggregation="interval_total", observed_at=record.date,
                    evidence_sha256=evidence_sha256, provider="deepseek", model=record.model,
                    scope="daily", dimensions={"source_format": "historical_csv"}))
        return [_finalize(item) for item in output], rejected, errors, incomplete

    data = json.loads(text, parse_float=Decimal)
    if kind == "manual-snapshot":
        record = parse_manual_template_json(data)
        if record.source_account_id != account:
            raise ValueError(
                f"manual snapshot account {record.source_account_id!r} does not match --account {account!r}"
            )
        for row in export_canonical_measurements(record):
            output.append(_from_canonical(row, source=source, account=account,
                                          evidence_sha=evidence_sha256, fallback_time=fallback))
    elif kind == "deepseek-balance":
        balances = data.get("balance_infos", data.get("balances", []))
        if not isinstance(balances, list):
            raise ValueError("DeepSeek balance input requires balance_infos list")
        for balance in balances:
            currency = str(balance.get("currency", "unknown")).lower()
            for metric in ("total_balance", "granted_balance", "topped_up_balance", "available_balance"):
                if balance.get(metric) is not None:
                    output.append(_observation(source="deepseek", account=account, metric=metric,
                        value=balance[metric], unit=currency, aggregation="gauge", observed_at=fallback,
                        evidence_sha256=evidence_sha256, provider="deepseek", dimensions={"currency": currency}))
    elif kind == "deepseek-usage":
        usage = data.get("usage") or data
        parsed = parse_inference_usage(usage, data.get("model", "deepseek-chat"))
        present = {key for key in ("prompt_tokens", "completion_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens") if key in usage}
        if "reasoning_tokens" in usage or any(
            isinstance(usage.get(details), dict) and "reasoning_tokens" in usage[details]
            for details in ("completion_tokens_details", "prompt_tokens_details")
        ):
            present.add("reasoning_tokens")
        for metric in ("prompt_tokens", "completion_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "reasoning_tokens"):
            if metric not in present:
                continue
            output.append(_observation(source="deepseek", account=account, metric=metric,
                value=parsed[metric], unit="token", aggregation="interval_total", observed_at=fallback,
                evidence_sha256=evidence_sha256, provider="deepseek", model=parsed["model"], scope="request"))
    elif kind == "byteplus-plan":
        collector = BytePlusCollector(profiles=[account])
        plan_data = data.get("data", data.get("result", data)) if isinstance(data, dict) else data
        observations = collector.parse_coding_plan_quota(account, plan_data, observed_at=fallback,
                                                          evidence_ref="sha256:" + evidence_sha256)
        for row in observations:
            if row.value is None:
                continue
            output.append(_observation(source="byteplus", account=account, metric=row.metric or row.capability,
                value=row.value, unit=row.unit or "request", aggregation="gauge", observed_at=row.observed_at,
                evidence_sha256=evidence_sha256, provider="byteplus", scope="coding_plan",
                dimensions={**row.dimensions, "product_domain": row.product_domain, "state": row.state},
                provenance_method="arkcli_capture"))
    elif kind in {"byteplus-stats", "byteplus-billing"}:
        product_domain = "inference" if kind == "byteplus-stats" else "billing"
        payload = data.get("data", data.get("result", data)) if isinstance(data, dict) else data
        incomplete = bool(
            data.get("is_truncated") or data.get("truncated")
            or (isinstance(payload, dict) and (payload.get("is_truncated") or payload.get("truncated")))
        )
        leaf_root = payload.get("items", payload.get("rows", payload)) if isinstance(payload, dict) else payload
        for metric, value in _numeric_leaves(leaf_root):
            if not _byteplus_metric_allowed(metric):
                continue
            leaf = metric.rsplit(".", 1)[-1]
            unit = "usd" if any(word in leaf.lower() for word in ("cost", "amount", "price")) else ("token" if "token" in leaf.lower() else "count")
            output.append(_observation(source="byteplus", account=account, metric=leaf,
                value=value, unit=unit, aggregation="interval_total", observed_at=fallback,
                evidence_sha256=evidence_sha256, provider="byteplus", scope=product_domain,
                dimensions={"product_domain": product_domain, "field_path": metric,
                            "complete": not incomplete}, provenance_method="arkcli_capture"))
    elif kind in {"kilo-session", "kilo-stats"}:
        adapter = KiloAdapter(account)
        if kind == "kilo-session":
            session, requests = adapter.parse_session(data)
            records = [session, *requests]
            raw_session = data.get("session", {})
            raw_messages = {item.get("id"): item for item in data.get("messages", []) if isinstance(item, dict)}
            for record in records:
                payload = asdict(record)
                timestamp = payload.get("updated_at_iso") or payload.get("created_at_iso") or fallback
                scope = payload["scope_level"]
                if scope == "session":
                    present_metrics = {name for name in ("tokens_input", "tokens_output", "tokens_reasoning", "tokens_cache_read", "tokens_cache_write") if name in raw_session}
                    raw_cost = raw_session.get("cost") if "cost" in raw_session else None
                else:
                    raw_message = raw_messages.get(payload.get("request_id"), {})
                    raw_tokens = raw_message.get("tokens", {}) if isinstance(raw_message.get("tokens", {}), dict) else {}
                    present_metrics = set()
                    for metric, raw_key in (("tokens_input", "input"), ("tokens_output", "output"), ("tokens_reasoning", "reasoning")):
                        if raw_key in raw_tokens:
                            present_metrics.add(metric)
                    raw_cache = raw_tokens.get("cache", {}) if isinstance(raw_tokens.get("cache", {}), dict) else {}
                    if "read" in raw_cache:
                        present_metrics.add("tokens_cache_read")
                    if "write" in raw_cache:
                        present_metrics.add("tokens_cache_write")
                    raw_cost = raw_message.get("cost") if "cost" in raw_message else None
                for metric in ("tokens_input", "tokens_output", "tokens_reasoning", "tokens_cache_read", "tokens_cache_write"):
                    if metric in present_metrics:
                        output.append(_observation(source="kilo", account=account, metric=metric,
                            value=payload[metric], unit="token", aggregation="interval_total", observed_at=timestamp,
                            evidence_sha256=evidence_sha256, provider=payload.get("provider_id"), model=payload.get("model_id"),
                            scope=scope, dimensions={key: payload.get(key) for key in ("session_id", "request_id", "council_id", "task_id") if payload.get(key)},
                            provenance_class="P1", provenance_method="kilo_export"))
                if raw_cost is not None:
                    output.append(_observation(source="kilo", account=account, metric="cost",
                        value=raw_cost, unit="usd", aggregation="interval_total", observed_at=timestamp,
                        evidence_sha256=evidence_sha256, provider=payload.get("provider_id"), model=payload.get("model_id"),
                        scope=scope, dimensions={"cost_semantic": payload.get("cost_semantic")},
                        provenance_class="P1", provenance_method="kilo_export"))
        else:
            payload = asdict(adapter.parse_stats(data))
            stats_data = data.get("stats", data)
            for metric in ("tokens_input", "tokens_output", "tokens_cache_read", "tokens_cache_write"):
                if metric not in stats_data:
                    continue
                output.append(_observation(source="kilo", account=account, metric=metric,
                    value=payload[metric], unit="token", aggregation="interval_total", observed_at=fallback,
                    evidence_sha256=evidence_sha256, scope="aggregate", dimensions={"days": payload.get("days")},
                    provenance_class="P1", provenance_method="kilo_stats"))
            raw_cost = stats_data.get("total_cost", stats_data.get("cost"))
            if raw_cost is not None:
                output.append(_observation(source="kilo", account=account, metric="cost",
                    value=raw_cost, unit="usd", aggregation="interval_total", observed_at=fallback,
                    evidence_sha256=evidence_sha256, scope="aggregate", dimensions={"days": payload.get("days")},
                    provenance_class="P1", provenance_method="kilo_stats"))
    elif kind == "canonical":
        rows = data if isinstance(data, list) else data.get("rows", data.get("measurements", []))
        if not isinstance(rows, list):
            raise ValueError("canonical input requires a list or rows/measurements list")
        for row in rows:
            try:
                row_account = row.get("source_account_id") or row.get("account")
                if row_account and row_account != account:
                    raise ValueError(
                        f"canonical row account {row_account!r} does not match --account {account!r}"
                    )
                output.append(_from_canonical(row, source=source, account=account,
                                              evidence_sha=evidence_sha256, fallback_time=fallback))
            except (KeyError, TypeError, ValueError) as exc:
                rejected += 1
                errors.append(str(exc))
    else:
        raise ValueError(f"unsupported import kind: {kind}")

    return [_finalize(item) for item in output], rejected, errors, incomplete
