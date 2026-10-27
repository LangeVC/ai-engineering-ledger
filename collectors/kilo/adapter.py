"""Kilo Collector / Adapter implementation (Python 3 stdlib).

Strict PRD US-10 adherence:
- US-10.a: Parse real minimized Kilo-Session-JSON with documented version. No missing fields guessed.
- US-10.b: Kilo aggregate stats and session requests are never summed when covering the same scope.
           Distinct provenance / scope levels ('aggregate' vs 'request').
- US-10.c: Provider, model, tokens, costs, latencies only normalized when present.
           Cost is marked execution_observation, never authoritative subscription billing.
- US-10.d: Retain project_id, session_id, repository_id, task_id, council_id when present.
           Never invent unknown IDs (remains None/null).
"""

from __future__ import annotations

import datetime
import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple, Union


class KiloParseError(ValueError):
    """Raised when JSON payload is corrupted or structurally invalid."""
    pass


class KiloValidationError(ValueError):
    """Raised when required fields are missing or invariants are violated."""
    pass


def _iso_from_ms(ms: Optional[int]) -> Optional[str]:
    """Convert millisecond timestamp to ISO-8601 UTC string without inventing timestamps."""
    if ms is None:
        return None
    try:
        dt = datetime.datetime.fromtimestamp(ms / 1000.0, tz=datetime.timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    except Exception:
        return None


def _compute_idempotency_key(parts: List[str]) -> str:
    """Compute stable SHA256 hex digest for idempotency key."""
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


@dataclass(frozen=True)
class KiloSessionRecord:
    """Session-level summary record."""
    session_id: str
    project_id: Optional[str]
    workspace_id: Optional[str]
    parent_id: Optional[str]
    council_id: Optional[str]
    task_id: Optional[str]
    title: Optional[str]
    tokens_input: int
    tokens_output: int
    tokens_reasoning: int
    tokens_cache_read: int
    tokens_cache_write: int
    cost_usd: float
    cost_semantic: str  # execution_observation, NOT subscription_billing
    created_at_iso: Optional[str]
    updated_at_iso: Optional[str]
    model_id: Optional[str]
    provider_id: Optional[str]
    agent: Optional[str]
    raw_source_version: str
    scope_level: str  # "session"
    idempotency_key: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class KiloRequestRecord:
    """Individual request / message record within a session."""
    request_id: str
    session_id: str
    project_id: Optional[str]
    council_id: Optional[str]
    task_id: Optional[str]
    seq: Optional[int]
    message_type: str
    agent: Optional[str]
    model_id: Optional[str]
    provider_id: Optional[str]
    tokens_input: int
    tokens_output: int
    tokens_reasoning: int
    tokens_cache_read: int
    tokens_cache_write: int
    cost_usd: float
    cost_semantic: str  # execution_observation, NOT subscription_billing
    latency_ms: Optional[float]
    created_at_iso: Optional[str]
    updated_at_iso: Optional[str]
    scope_level: str  # "request"
    idempotency_key: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class KiloAggregateRecord:
    """Aggregate statistics record from 'kilo stats'."""
    project_id: Optional[str]
    days: Optional[int]
    tokens_input: int
    tokens_output: int
    tokens_cache_read: int
    tokens_cache_write: int
    total_cost_usd: float
    cost_semantic: str  # execution_observation, NOT subscription_billing
    source_version: str
    scope_level: str  # "aggregate"
    idempotency_key: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def parse_kilo_export(
    raw_input: Union[str, bytes, Dict[str, Any]],
    external_council_id: Optional[str] = None,
    external_task_id: Optional[str] = None,
) -> Tuple[KiloSessionRecord, List[KiloRequestRecord]]:
    """Parse a Kilo session export JSON payload into a SessionRecord and RequestRecords.

    Strictly preserves IDs without inventing nonexistent values (US-10.d).
    Does NOT sum aggregate stats and requests (US-10.b).
    """
    if isinstance(raw_input, (str, bytes)):
        try:
            data = json.loads(raw_input)
        except Exception as e:
            raise KiloParseError(f"Invalid JSON: {e}") from e
    elif isinstance(raw_input, dict):
        data = raw_input
    else:
        raise KiloParseError(f"Expected str, bytes, or dict, got {type(raw_input).__name__}")

    if not isinstance(data, dict):
        raise KiloValidationError("Root JSON object must be a dictionary")

    source_version = str(data.get("version", "unknown"))

    session_data = data.get("session")
    if not isinstance(session_data, dict):
        raise KiloValidationError("Missing or invalid 'session' object in Kilo export")

    session_id = session_data.get("id")
    if not session_id or not isinstance(session_id, str):
        raise KiloValidationError("Missing required 'session.id'")

    # Invariant US-10.d: Do NOT invent IDs
    project_id = session_data.get("project_id") or None
    workspace_id = session_data.get("workspace_id") or None
    parent_id = session_data.get("parent_id") or None

    # Council / Task correlation: only from payload or explicit external parameter
    council_id = session_data.get("council_id") or external_council_id or None
    task_id = session_data.get("task_id") or external_task_id or None

    # Tokens check
    def _parse_non_neg_int(val: Any, name: str) -> int:
        if val is None:
            return 0
        if not isinstance(val, int) or val < 0:
            raise KiloValidationError(f"Invalid non-negative integer for {name}: {val}")
        return val

    tokens_input = _parse_non_neg_int(session_data.get("tokens_input", 0), "tokens_input")
    tokens_output = _parse_non_neg_int(session_data.get("tokens_output", 0), "tokens_output")
    tokens_reasoning = _parse_non_neg_int(session_data.get("tokens_reasoning", 0), "tokens_reasoning")
    tokens_cache_read = _parse_non_neg_int(session_data.get("tokens_cache_read", 0), "tokens_cache_read")
    tokens_cache_write = _parse_non_neg_int(session_data.get("tokens_cache_write", 0), "tokens_cache_write")

    cost = float(session_data.get("cost", 0.0) or 0.0)
    if cost < 0:
        raise KiloValidationError(f"Negative cost not allowed: {cost}")

    # Model and provider normalization
    model_obj = session_data.get("model")
    model_id: Optional[str] = None
    provider_id: Optional[str] = None
    if isinstance(model_obj, dict):
        model_id = model_obj.get("id") or None
        provider_id = model_obj.get("providerID") or None
    elif isinstance(model_obj, str):
        model_id = model_obj

    session_idem_key = _compute_idempotency_key([
        "kilo",
        "session",
        session_id,
        str(session_data.get("time_created", "")),
        str(session_data.get("time_updated", "")),
    ])

    session_record = KiloSessionRecord(
        session_id=session_id,
        project_id=project_id,
        workspace_id=workspace_id,
        parent_id=parent_id,
        council_id=council_id,
        task_id=task_id,
        title=session_data.get("title") or None,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        tokens_reasoning=tokens_reasoning,
        tokens_cache_read=tokens_cache_read,
        tokens_cache_write=tokens_cache_write,
        cost_usd=cost,
        cost_semantic="execution_observation",
        created_at_iso=_iso_from_ms(session_data.get("time_created")),
        updated_at_iso=_iso_from_ms(session_data.get("time_updated")),
        model_id=model_id,
        provider_id=provider_id,
        agent=session_data.get("agent") or None,
        raw_source_version=source_version,
        scope_level="session",
        idempotency_key=session_idem_key,
    )

    # Parse messages / requests
    request_records: List[KiloRequestRecord] = []
    messages = data.get("messages", [])
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            msg_id = msg.get("id")
            if not msg_id:
                continue

            msg_type = str(msg.get("type", "unknown"))
            # Only assistant messages represent LLM request execution observations with tokens
            # but other message types are tracked if they carry token metrics
            tokens_dict = msg.get("tokens") or {}
            msg_input = 0
            msg_output = 0
            msg_reasoning = 0
            msg_cache_read = 0
            msg_cache_write = 0

            if isinstance(tokens_dict, dict) and tokens_dict:
                msg_input = _parse_non_neg_int(tokens_dict.get("input", 0), f"msg.{msg_id}.input")
                msg_output = _parse_non_neg_int(tokens_dict.get("output", 0), f"msg.{msg_id}.output")
                msg_reasoning = _parse_non_neg_int(tokens_dict.get("reasoning", 0), f"msg.{msg_id}.reasoning")
                cache_dict = tokens_dict.get("cache") or {}
                if isinstance(cache_dict, dict):
                    msg_cache_read = _parse_non_neg_int(cache_dict.get("read", 0), f"msg.{msg_id}.cache.read")
                    msg_cache_write = _parse_non_neg_int(cache_dict.get("write", 0), f"msg.{msg_id}.cache.write")

            msg_cost = float(msg.get("cost", 0.0) or 0.0)
            if msg_cost < 0:
                raise KiloValidationError(f"Negative cost in message: {msg_cost}")

            # Latency if available
            latency_ms: Optional[float] = None
            if "latency_ms" in msg and msg["latency_ms"] is not None:
                latency_ms = float(msg["latency_ms"])
            elif msg.get("time_created") and msg.get("time_updated"):
                diff = msg["time_updated"] - msg["time_created"]
                if diff >= 0:
                    latency_ms = float(diff)

            msg_model_obj = msg.get("model")
            msg_model_id = model_id
            msg_provider_id = provider_id
            if isinstance(msg_model_obj, dict):
                msg_model_id = msg_model_obj.get("id") or msg_model_id
                msg_provider_id = msg_model_obj.get("providerID") or msg_provider_id
            elif isinstance(msg_model_obj, str):
                msg_model_id = msg_model_obj

            msg_idem_key = _compute_idempotency_key([
                "kilo",
                "request",
                session_id,
                msg_id,
                str(msg.get("time_created", "")),
            ])

            req_record = KiloRequestRecord(
                request_id=msg_id,
                session_id=session_id,
                project_id=project_id,
                council_id=council_id,
                task_id=task_id,
                seq=msg.get("seq"),
                message_type=msg_type,
                agent=msg.get("agent") or session_record.agent,
                model_id=msg_model_id,
                provider_id=msg_provider_id,
                tokens_input=msg_input,
                tokens_output=msg_output,
                tokens_reasoning=msg_reasoning,
                tokens_cache_read=msg_cache_read,
                tokens_cache_write=msg_cache_write,
                cost_usd=msg_cost,
                cost_semantic="execution_observation",
                latency_ms=latency_ms,
                created_at_iso=_iso_from_ms(msg.get("time_created")),
                updated_at_iso=_iso_from_ms(msg.get("time_updated")),
                scope_level="request",
                idempotency_key=msg_idem_key,
            )
            request_records.append(req_record)

    return session_record, request_records


def parse_kilo_stats(
    raw_input: Union[str, bytes, Dict[str, Any]],
) -> KiloAggregateRecord:
    """Parse aggregate statistics from 'kilo stats' output/JSON.

    Strictly tagged with scope_level='aggregate' (US-10.b).
    """
    if isinstance(raw_input, (str, bytes)):
        try:
            data = json.loads(raw_input)
        except Exception as e:
            raise KiloParseError(f"Invalid JSON: {e}") from e
    elif isinstance(raw_input, dict):
        data = raw_input
    else:
        raise KiloParseError(f"Expected str, bytes, or dict, got {type(raw_input).__name__}")

    source_version = str(data.get("version", "unknown"))
    stats_data = data.get("stats") if "stats" in data else data

    if not isinstance(stats_data, dict):
        raise KiloValidationError("Stats data must be a dictionary")

    project_id = stats_data.get("project_id") or None
    days = stats_data.get("days")
    if days is not None:
        if not isinstance(days, int) or days < 0:
            raise KiloValidationError(f"Invalid days value: {days}")

    def _get_int(key: str) -> int:
        v = stats_data.get(key, 0) or 0
        if not isinstance(v, int) or v < 0:
            raise KiloValidationError(f"Invalid non-negative int for {key}: {v}")
        return v

    cost = float(stats_data.get("total_cost", 0.0) or stats_data.get("cost", 0.0) or 0.0)
    if cost < 0:
        raise KiloValidationError(f"Negative cost in stats: {cost}")

    idem_key = _compute_idempotency_key([
        "kilo",
        "aggregate",
        str(project_id or "global"),
        str(days or "all"),
        str(cost),
    ])

    return KiloAggregateRecord(
        project_id=project_id,
        days=days,
        tokens_input=_get_int("tokens_input"),
        tokens_output=_get_int("tokens_output"),
        tokens_cache_read=_get_int("tokens_cache_read"),
        tokens_cache_write=_get_int("tokens_cache_write"),
        total_cost_usd=cost,
        cost_semantic="execution_observation",
        source_version=source_version,
        scope_level="aggregate",
        idempotency_key=idem_key,
    )


class KiloAdapter:
    """High-level Kilo Adapter verifying contract invariants and isolation."""

    def __init__(self, account_alias: str = "local-kilo") -> None:
        self.account_alias = account_alias

    def parse_session(
        self,
        raw_json: Union[str, bytes, Dict[str, Any]],
        external_council_id: Optional[str] = None,
        external_task_id: Optional[str] = None,
    ) -> Tuple[KiloSessionRecord, List[KiloRequestRecord]]:
        return parse_kilo_export(
            raw_json,
            external_council_id=external_council_id,
            external_task_id=external_task_id,
        )

    def parse_stats(
        self,
        raw_json: Union[str, bytes, Dict[str, Any]],
    ) -> KiloAggregateRecord:
        return parse_kilo_stats(raw_json)

    @staticmethod
    def assert_aggregation_safety(
        records: List[Union[KiloSessionRecord, KiloRequestRecord, KiloAggregateRecord]],
    ) -> None:
        """Enforces US-10.b: Kilo aggregate stats and session requests must NOT be summed

        together if covering the same scope. Raises KiloValidationError if mixed summing
        is attempted without scope separation.
        """
        has_aggregate = any(isinstance(r, KiloAggregateRecord) for r in records)
        has_requests = any(isinstance(r, KiloRequestRecord) for r in records)
        has_sessions = any(isinstance(r, KiloSessionRecord) for r in records)

        if has_aggregate and (has_requests or has_sessions):
            raise KiloValidationError(
                "Invariant US-10.b violation: Kilo aggregate stats and session requests "
                "cannot be combined in an unsegregated token sum."
            )
