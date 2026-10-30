"""
Faigate Collector and Adapter Implementation (Stdlib Python 3, sqlite3).
Extracts, maps, and normalizes gateway requests from the faigate SQLite database (Schema 2.10.2).
Strictly adheres to:
- PRD US-10 (No billing assertion from gateway observations, preserving IDs, no prompt leakage).
- PRD US-15 (Reconciliation invariants).
- Schema/collector contracts (account, capability, run, observation, event).
"""

from dataclasses import dataclass, field
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

from .reconcile import calculate_reconciliation, ReconciliationMetric


@dataclass
class FaigateConfig:
    db_path: str = "/opt/homebrew/var/lib/faigate/faigate.db"
    account_alias: str = "faigate_local"
    profile_id: str = "faigate_default"
    source: str = "faigate"
    product_domain: str = "runtime"
    batch_size: int = 500


@dataclass
class FaigateRequestRecord:
    id: int
    timestamp: float
    iso_time: str
    provider: str
    model: str
    layer: str
    rule_name: str
    prompt_tok: int
    compl_tok: int
    cache_hit: int
    cache_miss: int
    cost_usd: float  # Estimated gateway cost, NEVER subscription truth
    latency_ms: float
    success: int
    error: str
    requested_model: str
    modality: str
    client_profile: str
    client_tag: str
    decision_reason: str
    confidence: float
    canonical_model: str
    lane_family: str
    route_type: str
    lane_cluster: str
    selection_path: str
    runtime_window_state: str
    recovered_recently: int
    last_recovered_issue_type: str
    decision_details: Dict[str, Any]
    attempt_order: List[str]
    route_summary: Dict[str, Any]
    # Invariant-mapped correlation IDs:
    session_id: Optional[str]
    project_id: Optional[str]
    council_id: Optional[str]
    task_id: Optional[str]


@dataclass
class CollectionResult:
    status: str  # "success", "partial_failure", "failure"
    exit_status: int  # 0, 3, 1 (per ADR-0002)
    observations: List[Dict[str, Any]] = field(default_factory=list)
    events: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[Dict[str, Any]] = field(default_factory=list)
    capabilities: List[Dict[str, Any]] = field(default_factory=list)
    account_run: Optional[Dict[str, Any]] = None
    run_record: Optional[Dict[str, Any]] = None
    records: List[FaigateRequestRecord] = field(default_factory=list)
    totals: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ReconciliationResult:
    is_comparable: bool
    status: str
    metrics: Dict[str, ReconciliationMetric]
    summary_text: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_comparable": self.is_comparable,
            "status": self.status,
            "metrics": {k: v.to_dict() for k, v in self.metrics.items()},
            "summary_text": self.summary_text,
        }


def format_iso_timestamp(ts: Union[float, int, datetime]) -> str:
    """Returns ISO 8601 UTC timestamp string with Z indicator."""
    if isinstance(ts, (int, float)):
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    elif isinstance(ts, datetime):
        if ts.tzinfo is None:
            dt = ts.replace(tzinfo=timezone.utc)
        else:
            dt = ts.astimezone(timezone.utc)
    else:
        raise ValueError(f"Unsupported timestamp type: {type(ts)}")
    return dt.isoformat().replace("+00:00", "Z")


def generate_idempotency_key(source: str, account: str, entity_type: str, entity_id: str, scope: str = "") -> str:
    """Computes stable sha256 idempotency key per AEL event/measurement schema."""
    raw = f"{source}:{account}:{entity_type}:{entity_id}:{scope}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def extract_correlation_ids(client_tag: str, route_summary: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """
    Carefully maps client_tag and route_summary without hallucinating correlation.
    Per PRD US-10.d:
    - If client_tag explicitly encodes ses_*, session_id is captured.
    - If client_tag or route_summary explicitly encodes project_*, project_id is captured.
    - council_id and task_id stay None unless explicitly embedded.
    - Working directory or nearby files do NOT establish correlation.
    """
    session_id: Optional[str] = None
    project_id: Optional[str] = None
    council_id: Optional[str] = None
    task_id: Optional[str] = None

    tag = (client_tag or "").strip()
    # Check for embedded patterns e.g. ses_..., proj_...
    for token in tag.replace("/", " ").replace(":", " ").replace("-", " ").split():
        if token.startswith("ses_") and len(token) > 8:
            session_id = token
        elif token.startswith("proj_") or token.startswith("prj_"):
            project_id = token
        elif token.startswith("council_"):
            council_id = token
        elif token.startswith("task_"):
            task_id = token

    def explicit_string(value: Any) -> Optional[str]:
        return value.strip() if isinstance(value, str) and value.strip() else None

    # Check route_summary if dict. Nulls, numbers and inferred nearby context are not IDs.
    if isinstance(route_summary, dict):
        if not session_id and "session_id" in route_summary:
            session_id = explicit_string(route_summary["session_id"])
        if not project_id and "project_id" in route_summary:
            project_id = explicit_string(route_summary["project_id"])
        if not council_id and "council_id" in route_summary:
            council_id = explicit_string(route_summary["council_id"])
        if not task_id and "task_id" in route_summary:
            task_id = explicit_string(route_summary["task_id"])

    return session_id, project_id, council_id, task_id


class FaigateAdapter:
    """
    Native stdlib Python 3 + sqlite3 adapter for faigate 2.10.2 requests.
    """

    def __init__(self, config: Optional[FaigateConfig] = None):
        self.config = config or FaigateConfig()

    def get_connection(self) -> sqlite3.Connection:
        if not os.path.exists(self.config.db_path):
            raise FileNotFoundError(f"Faigate database not found at {self.config.db_path}")
        con = sqlite3.connect(f"file:{self.config.db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con

    def probe_health(self) -> Dict[str, Any]:
        """Probes the database file and table availability in read-only mode."""
        checked_at = format_iso_timestamp(datetime.now(timezone.utc))
        if not os.path.exists(self.config.db_path):
            return {
                "health": "unverified",
                "reason": f"faigate database does not exist at {self.config.db_path}",
                "checked_at": checked_at,
                "read_only_probe": True,
            }
        try:
            with self.get_connection() as con:
                row = con.execute("SELECT count(*) FROM requests").fetchone()
                row_count = row[0] if row else 0
                return {
                    "health": "ready",
                    "db_path": self.config.db_path,
                    "requests_count": row_count,
                    "checked_at": checked_at,
                    "read_only_probe": True,
                }
        except Exception as e:
            return {
                "health": "degraded",
                "reason": f"Failed to probe faigate.db: {str(e)}",
                "checked_at": checked_at,
                "read_only_probe": True,
            }

    def capability_report(self) -> Dict[str, Any]:
        health = self.probe_health()
        return {
            "schema_version": "0.3.0",
            "source": self.config.source,
            "account_alias": self.config.account_alias,
            "capability": "runtime_execution_observation",
            "status": health["health"],
            "read_only": True,
            "billing_truth": False,
            "correlation_policy": "explicit_ids_only",
            "health": health,
        }

    def dry_run(
        self,
        since_id: int = 0,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        client_tag_filter: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Return a metadata-only collection preview; the adapter is always read-only."""
        result = self.collect(since_id, start_time, end_time, client_tag_filter, limit)
        coverage = result.account_run.get("coverage") if result.account_run else None
        return {
            "status": result.status,
            "dry_run": True,
            "writes": 0,
            "capability": self.capability_report(),
            "coverage": coverage,
            "records": len(result.records),
            "idempotency_keys": [event["idempotency_key"] for event in result.events],
            "truth_semantics": "execution_observation",
            "billing_truth": False,
        }

    def read_requests(
        self,
        since_id: int = 0,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        client_tag_filter: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[FaigateRequestRecord]:
        """
        Reads requests directly from sqlite3 requests table.
        """
        query = ["SELECT * FROM requests WHERE id > ?"]
        params: List[Any] = [since_id]

        if start_time is not None:
            query.append("AND timestamp >= ?")
            params.append(start_time)
        if end_time is not None:
            query.append("AND timestamp <= ?")
            params.append(end_time)
        if client_tag_filter is not None:
            query.append("AND client_tag = ?")
            params.append(client_tag_filter)

        query.append("ORDER BY id ASC")
        if limit is not None:
            query.append("LIMIT ?")
            params.append(limit)

        sql = " ".join(query)
        records: List[FaigateRequestRecord] = []

        with self.get_connection() as con:
            for row in con.execute(sql, params):
                # parse json fields safely
                dec_details = {}
                att_order = []
                rt_summary = {}
                try:
                    raw_dd = row["decision_details"]
                    if raw_dd:
                        dec_details = json.loads(raw_dd)
                except Exception:
                    dec_details = {}

                try:
                    raw_ao = row["attempt_order"]
                    if raw_ao:
                        att_order = json.loads(raw_ao)
                except Exception:
                    att_order = []

                try:
                    raw_rs = row["route_summary"]
                    if raw_rs:
                        rt_summary = json.loads(raw_rs)
                except Exception:
                    rt_summary = {}

                ses_id, prj_id, cnl_id, tsk_id = extract_correlation_ids(row["client_tag"], rt_summary)

                ts = float(row["timestamp"])
                rec = FaigateRequestRecord(
                    id=row["id"],
                    timestamp=ts,
                    iso_time=format_iso_timestamp(ts),
                    provider=row["provider"] or "",
                    model=row["model"] or "",
                    layer=row["layer"] or "",
                    rule_name=row["rule_name"] or "",
                    prompt_tok=row["prompt_tok"] or 0,
                    compl_tok=row["compl_tok"] or 0,
                    cache_hit=row["cache_hit"] or 0,
                    cache_miss=row["cache_miss"] or 0,
                    cost_usd=float(row["cost_usd"] or 0.0),
                    latency_ms=float(row["latency_ms"] or 0.0),
                    success=row["success"] if row["success"] is not None else 1,
                    error=row["error"] or "",
                    requested_model=row["requested_model"] or "",
                    modality=row["modality"] or "",
                    client_profile=row["client_profile"] or "generic",
                    client_tag=row["client_tag"] or "",
                    decision_reason=row["decision_reason"] or "",
                    confidence=float(row["confidence"] or 0.0),
                    canonical_model=row["canonical_model"] or "",
                    lane_family=row["lane_family"] or "",
                    route_type=row["route_type"] or "",
                    lane_cluster=row["lane_cluster"] or "",
                    selection_path=row["selection_path"] or "",
                    runtime_window_state=row["runtime_window_state"] or "",
                    recovered_recently=row["recovered_recently"] or 0,
                    last_recovered_issue_type=row["last_recovered_issue_type"] or "",
                    decision_details=dec_details,
                    attempt_order=att_order,
                    route_summary=rt_summary,
                    session_id=ses_id,
                    project_id=prj_id,
                    council_id=cnl_id,
                    task_id=tsk_id,
                )
                records.append(rec)

        return records

    def collect(
        self,
        since_id: int = 0,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        client_tag_filter: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> CollectionResult:
        """
        Runs collection and packages results conforming to collector and event schemas.
        """
        started_at = format_iso_timestamp(datetime.now(timezone.utc))
        health = self.probe_health()

        if health["health"] != "ready":
            finished_at = format_iso_timestamp(datetime.now(timezone.utc))
            err = {
                "schema_version": "0.1",
                "error_id": "ERR_FAIGATE_HEALTH",
                "source": self.config.source,
                "account_alias": self.config.account_alias,
                "product_domain": self.config.product_domain,
                "classification": "unverified",
                "category": "environment",
                "message": health.get("reason", "Faigate database not ready"),
                "occurred_at": finished_at,
                "retryable": False,
            }
            return CollectionResult(
                status="failure",
                exit_status=1,
                errors=[err],
                run_record={
                    "schema_version": "0.1",
                    "run_id": f"run_faigate_{int(datetime.now(timezone.utc).timestamp())}",
                    "trigger": "manual",
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "status": "failure",
                    "exit_status": 1,
                    "routing_subset": [client_tag_filter] if client_tag_filter else [],
                    "account_runs": [
                        {
                            "account_alias": self.config.account_alias,
                            "profile_id": self.config.profile_id,
                            "source": self.config.source,
                            "product_domain": self.config.product_domain,
                            "capability": "runtime",
                            "status": "failure",
                            "observed_at": finished_at,
                            "coverage": {
                                "requested": {"start": started_at, "end": finished_at},
                                "delivered": None,
                                "confirmed": None,
                                "confirmation": "unconfirmed",
                            },
                            "errors": [err],
                        }
                    ],
                },
            )

        try:
            records = self.read_requests(
                since_id=since_id,
                start_time=start_time,
                end_time=end_time,
                client_tag_filter=client_tag_filter,
                limit=limit,
            )
        except Exception as e:
            finished_at = format_iso_timestamp(datetime.now(timezone.utc))
            err = {
                "schema_version": "0.1",
                "error_id": "ERR_FAIGATE_READ",
                "source": self.config.source,
                "account_alias": self.config.account_alias,
                "product_domain": self.config.product_domain,
                "classification": "transient_query_error",
                "category": "database",
                "message": f"Failed reading requests: {str(e)}",
                "occurred_at": finished_at,
                "retryable": True,
            }
            return CollectionResult(
                status="failure",
                exit_status=1,
                errors=[err],
            )

        finished_at = format_iso_timestamp(datetime.now(timezone.utc))

        # Build events & observations for records
        events: List[Dict[str, Any]] = []
        observations: List[Dict[str, Any]] = []

        total_requests = len(records)
        total_prompt_tok = 0
        total_compl_tok = 0
        total_cache_hit = 0
        total_cache_miss = 0
        total_cost_usd = 0.0
        total_success = 0
        total_failures = 0

        for r in records:
            total_prompt_tok += r.prompt_tok
            total_compl_tok += r.compl_tok
            total_cache_hit += r.cache_hit
            total_cache_miss += r.cache_miss
            total_cost_usd += r.cost_usd
            if r.success == 1:
                total_success += 1
            else:
                total_failures += 1

            idem_key = generate_idempotency_key(
                source=self.config.source,
                account=self.config.account_alias,
                entity_type="gateway_request",
                entity_id=str(r.id),
                scope=r.canonical_model or r.model,
            )

            # Execution Truth Event
            evt = {
                "idempotency_key": idem_key,
                "event_type": "gateway_execution_observation",
                "source": self.config.source,
                "account_alias": self.config.account_alias,
                "product_domain": self.config.product_domain,
                "observed_at": r.iso_time,
                "provider": r.provider,
                "model": r.model,
                "canonical_model": r.canonical_model,
                "client_profile": r.client_profile,
                "client_tag": r.client_tag,
                "session_id": r.session_id,
                "project_id": r.project_id,
                "council_id": r.council_id,
                "task_id": r.task_id,
                "tokens": {
                    "prompt": r.prompt_tok,
                    "completion": r.compl_tok,
                    "cache_hit": r.cache_hit,
                    "cache_miss": r.cache_miss,
                },
                "latency_ms": r.latency_ms,
                "estimated_cost_usd": r.cost_usd,
                "success": bool(r.success),
                "error": r.error,
                "billing_assertion": False,  # Strict US-10 invariant: No billing assertion
            }
            events.append(evt)

        # Observations for aggregate run
        ev_ref = f"ev_faigate_{int(datetime.now(timezone.utc).timestamp())}"
        obs_state = "value" if total_requests > 0 else "evidenced_zero"
        observations.append({
            "state": obs_state,
            "value": total_requests,
            "unit": "request",
            "observed_at": finished_at,
            "account_alias": self.config.account_alias,
            "product_domain": self.config.product_domain,
            "evidence_ref": ev_ref,
        })
        observations.append({
            "state": obs_state,
            "value": total_prompt_tok + total_compl_tok,
            "unit": "token",
            "observed_at": finished_at,
            "account_alias": self.config.account_alias,
            "product_domain": self.config.product_domain,
            "evidence_ref": ev_ref,
        })

        # Capability declaration
        cap = {
            "schema_version": "0.1",
            "source": self.config.source,
            "account_alias": self.config.account_alias,
            "product_domain": self.config.product_domain,
            "capability": "runtime",
            "status": "ready",
            "profile_binding": {
                "profile_id": self.config.profile_id,
                "explicit": True,
                "global_state_mutated": False,
            },
            "checked_at": finished_at,
            "read_only_probe": True,
            "evidence_ref": ev_ref,
        }

        first_iso = records[0].iso_time if records else started_at
        last_iso = records[-1].iso_time if records else finished_at

        # Requested coverage interval must envelop delivered interval per schema semantic rules
        req_start = min(started_at, first_iso)
        req_end = max(finished_at, last_iso)

        # Account run record
        account_run = {
            "account_alias": self.config.account_alias,
            "profile_id": self.config.profile_id,
            "source": self.config.source,
            "product_domain": self.config.product_domain,
            "capability": "runtime",
            "status": "success",
            "observed_at": finished_at,
            "coverage": {
                "requested": {"start": req_start, "end": req_end},
                "delivered": {"start": first_iso, "end": last_iso},
                "confirmed": {"start": first_iso, "end": last_iso},
                "confirmation": "complete",
                "pagination": {
                    "pages_fetched": 1,
                    "complete": True,
                    "truncated": False,
                    "next_cursor": None,
                },
            },
            "errors": [],
        }

        run_record = {
            "schema_version": "0.1",
            "run_id": f"run_faigate_{int(datetime.now(timezone.utc).timestamp())}",
            "trigger": "manual",
            "started_at": req_start,
            "finished_at": req_end,
            "status": "success",
            "exit_status": 0,
            "routing_subset": [client_tag_filter] if client_tag_filter else [],
            "account_runs": [account_run],
        }

        totals = {
            "total_requests": total_requests,
            "total_prompt_tok": total_prompt_tok,
            "total_compl_tok": total_compl_tok,
            "total_cache_hit": total_cache_hit,
            "total_cache_miss": total_cache_miss,
            "total_cost_usd": total_cost_usd,
            "total_success": total_success,
            "total_failures": total_failures,
            "billing_truth": False,  # US-10 Invariant
        }

        return CollectionResult(
            status="success",
            exit_status=0,
            observations=observations,
            events=events,
            errors=[],
            capabilities=[cap],
            account_run=account_run,
            run_record=run_record,
            records=records,
            totals=totals,
        )

    def reconcile_with_provider(
        self,
        provider_name: str,
        provider_totals: Dict[str, Union[int, float]],
        client_tag_filter: Optional[str] = None,
        is_comparable: bool = True,
        incomparable_reason: Optional[str] = None,
    ) -> ReconciliationResult:
        """
        Reconciles faigate gateway observation against authoritative provider totals per US-15.
        """
        coll = self.collect(client_tag_filter=client_tag_filter)
        gw_totals = coll.totals

        if not is_comparable:
            return ReconciliationResult(
                is_comparable=False,
                status="not_comparable",
                metrics={},
                summary_text=incomparable_reason or "Comparison scopes incompatible",
            )

        metrics = {}
        # 1. request_coverage
        metrics["request_coverage"] = calculate_reconciliation(
            metric_name="request_coverage",
            observed=gw_totals.get("total_requests", 0),
            authoritative=provider_totals.get("total_requests", 0),
        )

        # 2. prompt_token_coverage
        metrics["prompt_token_coverage"] = calculate_reconciliation(
            metric_name="prompt_token_coverage",
            observed=gw_totals.get("total_prompt_tok", 0),
            authoritative=provider_totals.get("total_prompt_tok", 0),
        )

        # 3. compl_token_coverage
        metrics["compl_token_coverage"] = calculate_reconciliation(
            metric_name="compl_token_coverage",
            observed=gw_totals.get("total_compl_tok", 0),
            authoritative=provider_totals.get("total_compl_tok", 0),
        )

        # 4. cache_hit_coverage
        metrics["cache_hit_coverage"] = calculate_reconciliation(
            metric_name="cache_hit_coverage",
            observed=gw_totals.get("total_cache_hit", 0),
            authoritative=provider_totals.get("total_cache_hit", 0),
        )

        overall_status = "ok"
        for m in metrics.values():
            if m.status in ("deviation_undefined", "over_coverage"):
                overall_status = "warning"
                break
            elif m.status == "under_coverage":
                overall_status = "partial"

        return ReconciliationResult(
            is_comparable=True,
            status=overall_status,
            metrics=metrics,
            summary_text=f"Reconciliation against {provider_name}: overall status {overall_status}",
        )
