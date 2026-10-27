"""Runtime Collector & Normalizer Implementation (Pure Python 3 standard library).

Enforces:
- US-10.a: Import documented versions of Kilo (7.0.46) and Faigate (2.10.2).
           Never invent missing fields; only explicit derived metrics with clear formulas.
- US-10.b: Kilo-Stats as aggregates and session requests covering the same scope
           must NEVER be summed together. Provenance and scope levels remain distinct.
- US-10.c: Gateway observation is pure Execution Truth (billing_assertion=False).
           Costs are execution estimates, never authoritative subscription billing.
- US-10.d: ID preservation (session_id, project_id, repository_id, task_id, council_id).
           Missing IDs remain None.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple, Union


class RuntimeValidationError(ValueError):
    """Raised when validation fails or contract invariants are violated."""
    pass


class RuntimeScopeError(RuntimeValidationError):
    """Raised when incompatible scopes or duplicate aggregations are detected."""
    pass


class RuntimeAggregationError(RuntimeValidationError):
    """Raised when aggregate-level and request-level metrics are illegally summed."""
    pass


def _iso_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _compute_sha256(content: Union[str, bytes]) -> str:
    if isinstance(content, str):
        content = content.encode("utf-8")
    return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True)
class RuntimeObservation:
    """Canonical normalized runtime observation.

    Adheres strictly to US-10.c:
    - billing_assertion is ALWAYS False for runtime observations.
    - cost_semantic describes execution observation, never subscription truth.
    """
    observation_id: str
    source: str                    # "kilo" | "faigate"
    source_version: str            # "7.0.46" | "2.10.2"
    scope_level: str               # "session" | "request" | "aggregate"

    # Identifiers (US-10.d: preserved, never fabricated)
    session_id: Optional[str]
    project_id: Optional[str]
    repository_id: Optional[str]
    task_id: Optional[str]
    council_id: Optional[str]
    request_id: Optional[str]

    # Execution metrics (US-10.c: normalized only when present in source)
    tokens_input: Optional[int]
    tokens_output: Optional[int]
    tokens_reasoning: Optional[int]
    tokens_cache_read: Optional[int]
    tokens_cache_write: Optional[int]
    total_tokens: Optional[int]     # Formula: input + output + (cache_read/write if documented)

    latency_ms: Optional[float]

    # Cost & Billing assertion (US-10.c: billing_assertion=False)
    cost_usd: Optional[float]
    billing_assertion: bool        # MUST BE FALSE
    cost_semantic: str             # "execution_observation"

    # Routing / execution details
    provider: Optional[str]
    model: Optional[str]
    agent: Optional[str]
    success: Optional[bool]
    error: Optional[str]

    # Timestamps
    created_at_iso: Optional[str]
    observed_at_iso: Optional[str]

    # Audit & Idempotency
    idempotency_key: str
    provenance_details: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.billing_assertion is not False:
            raise RuntimeValidationError(
                "Invariant US-10.c violation: runtime observation must never make a billing assertion (billing_assertion must be False)."
            )
        if self.cost_semantic == "subscription_billing" or self.cost_semantic == "authoritative_invoice":
            raise RuntimeValidationError(
                f"Invariant US-10.c violation: cost_semantic '{self.cost_semantic}' illegal for runtime observation."
            )
        if self.scope_level not in ("session", "request", "aggregate"):
            raise RuntimeValidationError(
                f"Invalid scope_level '{self.scope_level}'. Expected 'session', 'request', or 'aggregate'."
            )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class RuntimeCollector:
    """Collector and normalizer for Kilo and Faigate runtime sources."""

    def __init__(self, account_alias: str = "runtime_default"):
        self.account_alias = account_alias

    def import_kilo_session(
        self,
        json_content: Union[str, bytes],
        external_council_id: Optional[str] = None,
        external_task_id: Optional[str] = None,
        external_repo_id: Optional[str] = None,
    ) -> Tuple[RuntimeObservation, List[RuntimeObservation]]:
        """Import a Kilo session JSON export (US-10.a, US-10.b, US-10.c, US-10.d).

        Returns:
            Tuple of (Session-level Observation, List of Request-level Observations).
        """
        if isinstance(json_content, bytes):
            json_content = json_content.decode("utf-8")
        try:
            data = json.loads(json_content)
        except Exception as e:
            raise RuntimeValidationError(f"Invalid JSON payload: {e}")

        # Documented version check (US-10.a)
        version = data.get("version")
        if not version:
            # Fallback to session.version if top-level version omitted
            session_obj = data.get("session") or {}
            version = session_obj.get("version", "unknown")

        session_data = data.get("session")
        if not session_data or not isinstance(session_data, dict):
            raise RuntimeValidationError("Missing or invalid 'session' object in Kilo payload.")

        session_id = session_data.get("id")
        if not session_id:
            raise RuntimeValidationError("Missing 'session.id' in Kilo session payload.")

        project_id = session_data.get("project_id")

        # Explicit external correlation if provided, else null (never guess from directory)
        council_id = data.get("council_id") or session_data.get("council_id") or external_council_id
        task_id = data.get("task_id") or session_data.get("task_id") or external_task_id
        repo_id = data.get("repository_id") or session_data.get("repository_id") or external_repo_id

        # Metrics extraction (no guessing)
        tokens_input = session_data.get("tokens_input")
        tokens_output = session_data.get("tokens_output")
        tokens_reasoning = session_data.get("tokens_reasoning")
        tokens_cache_read = session_data.get("tokens_cache_read")
        tokens_cache_write = session_data.get("tokens_cache_write")

        # Explicit derivation with documented formula (US-10.a)
        # formula: total_tokens = (tokens_input or 0) + (tokens_output or 0)
        total_tokens = None
        if tokens_input is not None or tokens_output is not None:
            total_tokens = (tokens_input or 0) + (tokens_output or 0)

        # Cost extraction (US-10.c: pure execution observation, billing_assertion=False)
        cost_usd = session_data.get("cost")
        if cost_usd is not None:
            cost_usd = float(cost_usd)

        # Model & Provider
        model_obj = session_data.get("model")
        model_id = None
        provider_id = None
        if isinstance(model_obj, dict):
            model_id = model_obj.get("id")
            provider_id = model_obj.get("providerID")
        elif isinstance(model_obj, str):
            model_id = model_obj

        agent = session_data.get("agent")

        created_ms = session_data.get("time_created")
        updated_ms = session_data.get("time_updated")
        created_iso = self._ms_to_iso(created_ms)
        updated_iso = self._ms_to_iso(updated_ms)

        # Stable idempotency key
        session_idem = _compute_sha256(f"kilo:{session_id}:session:{created_ms}")

        session_obs = RuntimeObservation(
            observation_id=f"kilo_ses_{session_id}",
            source="kilo",
            source_version=str(version),
            scope_level="session",
            session_id=session_id,
            project_id=project_id,
            repository_id=repo_id,
            task_id=task_id,
            council_id=council_id,
            request_id=None,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            tokens_reasoning=tokens_reasoning,
            tokens_cache_read=tokens_cache_read,
            tokens_cache_write=tokens_cache_write,
            total_tokens=total_tokens,
            latency_ms=float(updated_ms - created_ms) if (updated_ms and created_ms and updated_ms >= created_ms) else None,
            cost_usd=cost_usd,
            billing_assertion=False,  # US-10.c
            cost_semantic="execution_observation",
            provider=provider_id,
            model=model_id,
            agent=agent,
            success=True,
            error=None,
            created_at_iso=created_iso,
            observed_at_iso=updated_iso or created_iso,
            idempotency_key=session_idem,
            provenance_details={"slug": session_data.get("slug"), "directory": session_data.get("directory")},
        )

        # Parse messages / requests
        requests: List[RuntimeObservation] = []
        messages = data.get("messages") or []
        for msg in messages:
            msg_id = msg.get("id")
            if not msg_id:
                continue

            # Check for token metrics or tool/assistant invocations
            toks = msg.get("tokens") or {}
            m_input = toks.get("input")
            m_output = toks.get("output")
            m_reasoning = toks.get("reasoning")
            cache = toks.get("cache") or {}
            m_cache_read = cache.get("read")
            m_cache_write = cache.get("write")

            m_model_obj = msg.get("model") or model_obj
            m_model = None
            m_provider = None
            if isinstance(m_model_obj, dict):
                m_model = m_model_obj.get("id")
                m_provider = m_model_obj.get("providerID")
            elif isinstance(m_model_obj, str):
                m_model = m_model_obj

            m_cost = msg.get("cost")
            if m_cost is not None:
                m_cost = float(m_cost)

            m_created_ms = msg.get("time_created")
            m_updated_ms = msg.get("time_updated")
            m_latency = None
            if m_created_ms and m_updated_ms and m_updated_ms >= m_created_ms:
                m_latency = float(m_updated_ms - m_created_ms)

            m_total_tokens = None
            if m_input is not None or m_output is not None:
                m_total_tokens = (m_input or 0) + (m_output or 0)

            msg_idem = _compute_sha256(f"kilo:{session_id}:req:{msg_id}:{m_created_ms}")

            req_obs = RuntimeObservation(
                observation_id=f"kilo_req_{msg_id}",
                source="kilo",
                source_version=str(version),
                scope_level="request",
                session_id=session_id,
                project_id=project_id,
                repository_id=repo_id,
                task_id=task_id,
                council_id=council_id,
                request_id=msg_id,
                tokens_input=m_input,
                tokens_output=m_output,
                tokens_reasoning=m_reasoning,
                tokens_cache_read=m_cache_read,
                tokens_cache_write=m_cache_write,
                total_tokens=m_total_tokens,
                latency_ms=m_latency,
                cost_usd=m_cost,
                billing_assertion=False,  # US-10.c
                cost_semantic="execution_observation",
                provider=m_provider or provider_id,
                model=m_model or model_id,
                agent=msg.get("agent") or agent,
                success=True,
                error=None,
                created_at_iso=self._ms_to_iso(m_created_ms),
                observed_at_iso=self._ms_to_iso(m_updated_ms or m_created_ms),
                idempotency_key=msg_idem,
                provenance_details={"message_type": msg.get("type"), "seq": msg.get("seq")},
            )
            requests.append(req_obs)

        return session_obs, requests

    def import_kilo_stats(
        self,
        json_content: Union[str, bytes],
        version: str = "7.0.46",
    ) -> RuntimeObservation:
        """Import Kilo aggregate statistics (kilo stats) with distinct aggregate scope (US-10.b)."""
        if isinstance(json_content, bytes):
            json_content = json_content.decode("utf-8")
        try:
            data = json.loads(json_content)
        except Exception as e:
            raise RuntimeValidationError(f"Invalid JSON payload: {e}")

        stats = data.get("stats") if "stats" in data else data
        project_id = stats.get("project_id")
        days = stats.get("days")
        tokens_input = stats.get("tokens_input")
        tokens_output = stats.get("tokens_output")
        tokens_cache_read = stats.get("tokens_cache_read")
        cost_usd = stats.get("total_cost") or stats.get("cost_usd")
        if cost_usd is not None:
            cost_usd = float(cost_usd)

        total_tokens = None
        if tokens_input is not None or tokens_output is not None:
            total_tokens = (tokens_input or 0) + (tokens_output or 0)

        now_iso = _iso_now()
        idem_key = _compute_sha256(f"kilo_stats:{project_id}:{days}:{tokens_input}:{tokens_output}")

        return RuntimeObservation(
            observation_id=f"kilo_agg_{project_id or 'all'}_{days or 'all'}",
            source="kilo",
            source_version=version,
            scope_level="aggregate",  # US-10.b: distinct aggregate scope
            session_id=None,
            project_id=project_id,
            repository_id=None,
            task_id=None,
            council_id=None,
            request_id=None,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            tokens_reasoning=None,
            tokens_cache_read=tokens_cache_read,
            tokens_cache_write=None,
            total_tokens=total_tokens,
            latency_ms=None,
            cost_usd=cost_usd,
            billing_assertion=False,  # US-10.c
            cost_semantic="execution_observation",
            provider=None,
            model=None,
            agent=None,
            success=True,
            error=None,
            created_at_iso=now_iso,
            observed_at_iso=now_iso,
            idempotency_key=idem_key,
            provenance_details={"days": days, "source_type": "kilo_stats"},
        )

    def import_faigate_request(
        self,
        request_row: Dict[str, Any],
        version: str = "2.10.2",
    ) -> RuntimeObservation:
        """Import a faigate gateway observation row (US-10.a, US-10.c).

        Gateway observation is pure Execution Truth:
        - billing_assertion is strictly False
        - cost_usd is an execution observation / gateway estimate, not invoice truth.
        """
        req_id = request_row.get("id")
        if req_id is None:
            raise RuntimeValidationError("faigate request row missing 'id'")

        ts = request_row.get("timestamp")
        iso_time = self._ts_to_iso(ts)

        provider = request_row.get("provider")
        model = request_row.get("model") or request_row.get("canonical_model")

        prompt_tok = request_row.get("prompt_tok")
        compl_tok = request_row.get("compl_tok")
        cache_hit = request_row.get("cache_hit")
        cache_miss = request_row.get("cache_miss")

        # Explicit formula: total_tokens = prompt_tok + compl_tok
        total_tokens = None
        if prompt_tok is not None or compl_tok is not None:
            total_tokens = (prompt_tok or 0) + (compl_tok or 0)

        cost_usd = request_row.get("cost_usd")
        if cost_usd is not None:
            cost_usd = float(cost_usd)

        latency_ms = request_row.get("latency_ms")
        if latency_ms is not None:
            latency_ms = float(latency_ms)

        success_flag = bool(request_row.get("success", 1))
        error_msg = request_row.get("error") or None

        # US-10.d: Preserve IDs from client_tag or route_summary without hallucination
        client_tag = request_row.get("client_tag") or ""
        route_summary = request_row.get("route_summary") or {}
        if isinstance(route_summary, str):
            try:
                route_summary = json.loads(route_summary)
            except Exception:
                route_summary = {}

        session_id = None
        project_id = None
        council_id = None
        task_id = None

        # Extract only if explicitly present
        if "ses_" in client_tag:
            for part in client_tag.split():
                if part.startswith("ses_"):
                    session_id = part
                elif part.startswith("proj_"):
                    project_id = part
                elif part.startswith("cncl_"):
                    council_id = part
                elif part.startswith("task_"):
                    task_id = part

        if isinstance(route_summary, dict):
            session_id = route_summary.get("session_id") or session_id
            project_id = route_summary.get("project_id") or project_id
            council_id = route_summary.get("council_id") or council_id
            task_id = route_summary.get("task_id") or task_id

        idem_key = _compute_sha256(f"faigate:req:{req_id}:{ts}:{provider}:{model}")

        return RuntimeObservation(
            observation_id=f"faigate_req_{req_id}",
            source="faigate",
            source_version=version,
            scope_level="request",
            session_id=session_id,
            project_id=project_id,
            repository_id=None,
            task_id=task_id,
            council_id=council_id,
            request_id=str(req_id),
            tokens_input=prompt_tok,
            tokens_output=compl_tok,
            tokens_reasoning=None,
            tokens_cache_read=cache_hit,
            tokens_cache_write=None,
            total_tokens=total_tokens,
            latency_ms=latency_ms,
            cost_usd=cost_usd,
            billing_assertion=False,  # US-10.c: pure execution truth
            cost_semantic="execution_observation",
            provider=provider,
            model=model,
            agent=request_row.get("client_profile"),
            success=success_flag,
            error=error_msg,
            created_at_iso=iso_time,
            observed_at_iso=iso_time,
            idempotency_key=idem_key,
            provenance_details={
                "client_tag": client_tag,
                "layer": request_row.get("layer"),
                "rule_name": request_row.get("rule_name"),
                "lane_family": request_row.get("lane_family"),
            },
        )

    def import_faigate_export(
        self,
        json_content: Union[str, bytes],
    ) -> List[RuntimeObservation]:
        """Import faigate CLI / JSON export structure (US-10.a)."""
        if isinstance(json_content, bytes):
            json_content = json_content.decode("utf-8")
        try:
            data = json.loads(json_content)
        except Exception as e:
            raise RuntimeValidationError(f"Invalid JSON payload: {e}")

        version = data.get("version", "2.10.2")
        records: List[RuntimeObservation] = []

        # If it contains minimal_request_fixture
        if "minimal_request_fixture" in data:
            records.append(self.import_faigate_request(data["minimal_request_fixture"], version=version))

        # If it is a list of rows
        elif isinstance(data, list):
            for row in data:
                records.append(self.import_faigate_request(row, version=version))

        # If it has a requests array
        elif "requests" in data and isinstance(data["requests"], list):
            for row in data["requests"]:
                records.append(self.import_faigate_request(row, version=version))

        return records

    @staticmethod
    def _ms_to_iso(ms: Optional[int]) -> Optional[str]:
        if ms is None:
            return None
        try:
            dt = datetime.datetime.fromtimestamp(ms / 1000.0, tz=datetime.timezone.utc)
            return dt.isoformat().replace("+00:00", "Z")
        except Exception:
            return None

    @staticmethod
    def _ts_to_iso(ts: Optional[float]) -> Optional[str]:
        if ts is None:
            return None
        try:
            dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
            return dt.isoformat().replace("+00:00", "Z")
        except Exception:
            return None


def aggregate_runtime_observations(
    observations: List[RuntimeObservation],
    allow_mixed_sources: bool = True,
) -> Dict[str, Any]:
    """Aggregate runtime observations while enforcing Invariant US-10.b.

    Invariant US-10.b:
    - Kilo-Stats as aggregates and session requests covering the same scope
      MUST NOT be summed together.
    - If 'aggregate' scope records are present alongside 'session' or 'request'
      records for the same source/project, an explicit RuntimeAggregationError is raised.
    - Provenance / scope origin remains distinctly reported.
    """
    if not observations:
        return {
            "total_observations": 0,
            "total_tokens_input": 0,
            "total_tokens_output": 0,
            "total_tokens": 0,
            "total_cost_usd": 0.0,
            "billing_assertion": False,
            "scope_breakdown": {},
        }

    # Group by (source, project_id, scope_level)
    scope_levels: Set[str] = {obs.scope_level for obs in observations}

    # Check US-10.b invariant
    has_aggregate = "aggregate" in scope_levels
    has_granular = ("session" in scope_levels) or ("request" in scope_levels)

    if has_aggregate and has_granular:
        # Check if they share the same source or project scope
        sources_agg = {o.source for o in observations if o.scope_level == "aggregate"}
        sources_gran = {o.source for o in observations if o.scope_level in ("session", "request")}
        overlapping_sources = sources_agg.intersection(sources_gran)

        if overlapping_sources:
            raise RuntimeAggregationError(
                f"Invariant US-10.b violation: cannot sum aggregate-level and request/session-level observations "
                f"for source(s) {overlapping_sources}. Aggregate stats and individual requests covering the same scope "
                f"must remain segregated to prevent double-counting."
            )

    # Invariant: session and request within the same session must also not be summed together
    session_ids = {o.session_id for o in observations if o.session_id}
    has_session_scope = any(o.scope_level == "session" for o in observations)
    has_request_scope = any(o.scope_level == "request" for o in observations)

    if has_session_scope and has_request_scope:
        # Check if any request belongs to a session present in session-scope
        for obs in observations:
            if obs.scope_level == "request" and obs.session_id in session_ids:
                matching_sessions = [o for o in observations if o.scope_level == "session" and o.session_id == obs.session_id]
                if matching_sessions:
                    raise RuntimeAggregationError(
                        f"Invariant US-10.b violation: session '{obs.session_id}' has both session-level aggregate "
                        f"and individual request-level observations in the same aggregation pool. "
                        f"Summing both would cause double-counting."
                    )

    # Safe summation
    total_tokens_input = sum(o.tokens_input or 0 for o in observations)
    total_tokens_output = sum(o.tokens_output or 0 for o in observations)
    total_tokens_cache_read = sum(o.tokens_cache_read or 0 for o in observations)
    total_tokens = sum(o.total_tokens or 0 for o in observations)
    total_cost_usd = sum(o.cost_usd or 0.0 for o in observations)

    # Breakdown by scope & source
    breakdown: Dict[str, Dict[str, Any]] = {}
    for o in observations:
        key = f"{o.source}:{o.scope_level}"
        if key not in breakdown:
            breakdown[key] = {
                "source": o.source,
                "scope_level": o.scope_level,
                "count": 0,
                "tokens_input": 0,
                "tokens_output": 0,
                "cost_usd": 0.0,
                "billing_assertion": False,
            }
        breakdown[key]["count"] += 1
        breakdown[key]["tokens_input"] += (o.tokens_input or 0)
        breakdown[key]["tokens_output"] += (o.tokens_output or 0)
        breakdown[key]["cost_usd"] += (o.cost_usd or 0.0)

    return {
        "total_observations": len(observations),
        "total_tokens_input": total_tokens_input,
        "total_tokens_output": total_tokens_output,
        "total_tokens_cache_read": total_tokens_cache_read,
        "total_tokens": total_tokens,
        "total_cost_usd": round(total_cost_usd, 6),
        "billing_assertion": False,  # US-10.c: Always False for runtime
        "cost_semantic": "execution_observation",
        "scope_breakdown": breakdown,
    }


def reconcile_runtime_with_provider(
    observed_runtime_tokens: int,
    authoritative_provider_tokens: int,
    metric_name: str = "tokens",
) -> Dict[str, Any]:
    """Compare observed runtime execution truth with authoritative provider billing truth.

    Formula: coverage = observed / authoritative
    Adheres strictly to US-10.c and US-15:
    - Never equates execution truth with billing truth.
    - Reports clear coverage ratio and delta.
    """
    delta = observed_runtime_tokens - authoritative_provider_tokens

    if authoritative_provider_tokens == 0:
        if observed_runtime_tokens == 0:
            return {
                "metric": metric_name,
                "observed": 0,
                "authoritative": 0,
                "delta": 0,
                "coverage_ratio": None,
                "status": "no_usage",
                "notes": "Both runtime observed and provider authoritative report zero tokens.",
            }
        else:
            return {
                "metric": metric_name,
                "observed": observed_runtime_tokens,
                "authoritative": 0,
                "delta": delta,
                "coverage_ratio": None,
                "status": "deviation_undefined",
                "notes": "Observed runtime traffic without provider billing records.",
            }

    ratio = observed_runtime_tokens / authoritative_provider_tokens

    if ratio < 1.0:
        status = "under_coverage"
    elif ratio > 1.0:
        status = "over_coverage"
    else:
        status = "exact_match"

    return {
        "metric": metric_name,
        "observed": observed_runtime_tokens,
        "authoritative": authoritative_provider_tokens,
        "delta": delta,
        "coverage_ratio": round(ratio, 6),
        "status": status,
        "notes": f"Coverage {ratio:.4%}. Runtime observed is execution truth, not billing truth.",
    }
