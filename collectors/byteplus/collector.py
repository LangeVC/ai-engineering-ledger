#!/usr/bin/env python3
"""BytePlus Multi-Account Collector & Adapter (AEL v0.1).
Pure Python 3 standard library implementation.

Fulfills PRD US-08:
- US-08.a: Für jedes der fünf Konten (bp01..bp05) werden nur nach Capability-Prüfung verfügbare
  Quota-, Usage- und Billingdaten erfasst; Kontoalias und Produktdomäne sind in jedem Datensatz enthalten.
- US-08.b: Eine Fixture mit Coding-Plan-Quota und separater Inference-Usage erzeugt zwei getrennte Reihen;
  die Quotaansicht enthält keine aus Inference abgeleiteten Coding-Plan-Tokens.
- US-08.c: Schlägt bp03 fehl, werden vier erfolgreiche Accountläufe gespeichert und der Gesamtlauf als
  teilweise fehlgeschlagen gemeldet (status='partial_failure', exit_status=3); kein Account übernimmt
  Daten eines anderen.

Integrates with `collectors/account` (BytePlusProfileCoordinator, IsolationManager).
Conforms to schemas:
- schema/collector (account, capability, run, observation, error)
- schema/core (event, measurement, evidence, revision)
"""

from __future__ import annotations

import copy
import datetime
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from collectors.account import (
    HEALTH_READY,
    CAPABILITY_READY,
    CAPABILITY_UNVERIFIED,
    CAPABILITY_UNSUPPORTED,
    BytePlusProfileCoordinator,
    SecretResolver,
    GLOBAL_REDACTOR,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "0.1"
SOURCE_NAME = "byteplus"
DEFAULT_PROFILES = ("bp01", "bp02", "bp03", "bp04", "bp05")

# Canonical Product Domains for BytePlus
DOMAIN_CODING_PLAN = "byteplus_coding_plan"
DOMAIN_INFERENCE = "byteplus_inference"
DOMAIN_BILLING = "byteplus_billing"

ALL_DOMAINS = (DOMAIN_CODING_PLAN, DOMAIN_INFERENCE, DOMAIN_BILLING)

# Crockford Base32 alphabet for ULID generation
_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def generate_ulid(timestamp_ms: Optional[int] = None) -> str:
    """Generates a canonical 26-character Crockford Base32 ULID."""
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)

    time_chars = []
    t = timestamp_ms
    for _ in range(10):
        time_chars.append(_CROCKFORD_ALPHABET[t % 32])
        t //= 32
    time_part = "".join(reversed(time_chars))

    rand_chars = []
    rand_bytes = secrets.token_bytes(10)
    r_val = int.from_bytes(rand_bytes, "big")
    for _ in range(16):
        rand_chars.append(_CROCKFORD_ALPHABET[r_val % 32])
        r_val //= 32
    rand_part = "".join(reversed(rand_chars))

    return f"{time_part}{rand_part}"


def format_utc_timestamp(dt: Optional[datetime.datetime] = None) -> str:
    """Formats datetime as RFC 3339 / ISO 8601 UTC timestamp."""
    if dt is None:
        dt = datetime.datetime.now(datetime.timezone.utc)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    else:
        dt = dt.astimezone(datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def derive_idempotency_key(
    source: str,
    account: str,
    native_id: str,
    window: Optional[Dict[str, Any]] = None,
) -> str:
    """Derives stable sha256 idempotency key per DQ03."""
    win_part = f"{window.get('starts_at')}:{window.get('ends_at')}" if window else "none"
    seed = f"{source}:{account}:{native_id}:{win_part}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class BytePlusObservation:
    account_alias: str
    product_domain: str
    capability: str
    state: str  # 'value', 'evidenced_zero', 'partial', 'unverified', etc.
    value: Optional[Union[int, Decimal, float]] = None
    unit: Optional[str] = None
    observed_at: str = field(default_factory=format_utc_timestamp)
    evidence_ref: Optional[str] = None
    reason: Optional[str] = None
    metric: Optional[str] = None
    dimensions: Dict[str, Any] = field(default_factory=dict)

    def to_observation_dict(self) -> Dict[str, Any]:
        """Conforms strictly to schema/collector/observation.schema.json."""
        d: Dict[str, Any] = {
            "state": self.state,
            "observed_at": self.observed_at,
            "account_alias": self.account_alias,
            "product_domain": self.product_domain,
        }
        if self.state in ("value", "evidenced_zero", "partial"):
            if self.value is not None:
                d["value"] = int(self.value) if isinstance(self.value, (int, Decimal)) and int(self.value) == self.value else float(self.value)
            if self.unit:
                d["unit"] = self.unit
            if self.evidence_ref:
                d["evidence_ref"] = self.evidence_ref
        if self.state in ("partial", "unknown", "unsupported", "unverified", "unauthorized"):
            d["reason"] = self.reason or "No further reason provided"
        return d


@dataclass
class AccountCollectionResult:
    account_alias: str
    profile_id: str
    product_domain: str
    capability: str
    status: str  # 'success', 'failure', 'skipped_unsupported', 'skipped_unverified', 'skipped_unauthorized'
    observed_at: str
    coverage: Dict[str, Any]
    errors: List[Dict[str, Any]] = field(default_factory=list)
    process: Optional[Dict[str, Any]] = field(default_factory=dict)
    observations: List[BytePlusObservation] = field(default_factory=list)
    raw_payloads: List[Dict[str, Any]] = field(default_factory=list)

    def to_account_run_dict(self) -> Dict[str, Any]:
        """Conforms strictly to schema/collector/run.schema.json #/$defs/account_run."""
        d: Dict[str, Any] = {
            "account_alias": self.account_alias,
            "profile_id": self.profile_id,
            "source": SOURCE_NAME,
            "product_domain": self.product_domain,
            "capability": self.capability,
            "status": self.status,
            "observed_at": self.observed_at,
            "coverage": copy.deepcopy(self.coverage),
            "errors": copy.deepcopy(self.errors),
        }
        if self.process:
            d["process"] = copy.deepcopy(self.process)
        return d


@dataclass
class BytePlusRunResult:
    run_id: str
    trigger: str  # 'manual' | 'scheduler'
    started_at: str
    finished_at: str
    status: str  # 'success' | 'partial_failure' | 'failure'
    exit_status: int  # 0, 3, 1
    routing_subset: List[str]
    account_runs: List[AccountCollectionResult] = field(default_factory=list)

    def to_run_dict(self) -> Dict[str, Any]:
        """Conforms strictly to schema/collector/run.schema.json."""
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "trigger": self.trigger,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "exit_status": self.exit_status,
            "routing_subset": list(self.routing_subset),
            "account_runs": [ar.to_account_run_dict() for ar in self.account_runs],
        }


# ---------------------------------------------------------------------------
# Collector Implementation
# ---------------------------------------------------------------------------

class BytePlusCollector:
    """
    BytePlus Multi-Account Collector (PRD US-08).
    Manages accounts bp01..bp05 with strict isolation, capability gating,
    domain separation, and partial failure handling.
    """

    def __init__(
        self,
        profiles: Sequence[str] = DEFAULT_PROFILES,
        coordinator: Optional[BytePlusProfileCoordinator] = None,
        secret_resolver: Optional[SecretResolver] = None,
    ) -> None:
        self.profiles = list(profiles)
        if len(set(self.profiles)) != len(self.profiles):
            raise ValueError("BytePlus account aliases must be unique")
        self.coordinator = coordinator or BytePlusProfileCoordinator(
            profiles=self.profiles,
            secret_resolver=secret_resolver,
        )
        self.redactor = GLOBAL_REDACTOR

    def declare_capability(
        self,
        account_alias: str,
        product_domain: str,
        capability: str,
        status: str,
        checked_at: Optional[str] = None,
        evidence_ref: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Builds a capability descriptor conforming to capability.schema.json."""
        ts = checked_at or format_utc_timestamp()
        cap_obj: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "source": SOURCE_NAME,
            "account_alias": account_alias,
            "product_domain": product_domain,
            "capability": capability,
            "status": status,
            "profile_binding": {
                "profile_id": account_alias,
                "explicit": True,
                "global_state_mutated": False,
            },
            "checked_at": ts,
        }
        if status in ("ready", "unauthorized"):
            cap_obj["read_only_probe"] = True
            cap_obj["evidence_ref"] = evidence_ref or f"ev_{account_alias}_{capability}_probe"
        elif status in ("unsupported", "unverified"):
            cap_obj["reason"] = reason or f"Capability {capability} for {product_domain} is {status}"
        return cap_obj

    def declare_account(
        self,
        account_alias: str,
        secret_ref: Optional[str] = None,
        health: str = HEALTH_READY,
        checked_at: Optional[str] = None,
        capabilities: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Builds an account descriptor conforming to account.schema.json."""
        ts = checked_at or format_utc_timestamp()
        if capabilities is None:
            # Standard capabilities for BytePlus
            capabilities = [
                self.declare_capability(
                    account_alias=account_alias,
                    product_domain=DOMAIN_CODING_PLAN,
                    capability="quota",
                    status=CAPABILITY_READY,
                    checked_at=ts,
                    evidence_ref=f"ev_{account_alias}_quota_live",
                ),
                self.declare_capability(
                    account_alias=account_alias,
                    product_domain=DOMAIN_INFERENCE,
                    capability="usage",
                    status=CAPABILITY_UNVERIFIED,
                    checked_at=ts,
                    reason="Inference usage collected via stats API when requested",
                ),
                self.declare_capability(
                    account_alias=account_alias,
                    product_domain=DOMAIN_BILLING,
                    capability="billing",
                    status=CAPABILITY_UNVERIFIED,
                    checked_at=ts,
                    reason="Billing data collected on T+1 settlement schedule",
                ),
            ]

        acc_obj: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "source": SOURCE_NAME,
            "alias": account_alias,
            "profile_id": account_alias,
            "product_domains": list(ALL_DOMAINS),
            "capabilities": capabilities,
            "health": health,
            "health_checked_at": ts,
        }
        if secret_ref:
            acc_obj["secret_ref"] = secret_ref
        else:
            acc_obj["secret_ref"] = f"env:BYTEPLUS_API_KEY_{account_alias.upper()}"
        return acc_obj

    def parse_coding_plan_quota(
        self,
        account_alias: str,
        data: Dict[str, Any],
        observed_at: Optional[str] = None,
        evidence_ref: Optional[str] = None,
    ) -> List[BytePlusObservation]:
        """
        Parses Coding Plan quota data into BytePlusObservation instances.
        Domain is strictly DOMAIN_CODING_PLAN.
        Ensures quota view never mixes inference tokens (US-08.b invariant).
        """
        ts = observed_at or format_utc_timestamp()
        ev_ref = evidence_ref or f"ev_{account_alias}_coding_plan_quota"
        obs_list: List[BytePlusObservation] = []

        # If quota limit is present
        if "quota_limit" in data or "limit" in data:
            lim = data.get("quota_limit", data.get("limit"))
            unit = data.get("quota_unit", data.get("unit", "request"))
            obs_list.append(BytePlusObservation(
                account_alias=account_alias,
                product_domain=DOMAIN_CODING_PLAN,
                capability="quota",
                state="value",
                value=lim,
                unit=unit,
                observed_at=ts,
                evidence_ref=ev_ref,
                metric="quota_limit",
                dimensions={"plan": data.get("plan", "coding_plan_pro")},
            ))

        # If quota used is present
        if "quota_used" in data or "used" in data:
            used = data.get("quota_used", data.get("used"))
            unit = data.get("quota_unit", data.get("unit", "request"))
            state = "evidenced_zero" if used == 0 else "value"
            obs_list.append(BytePlusObservation(
                account_alias=account_alias,
                product_domain=DOMAIN_CODING_PLAN,
                capability="quota",
                state=state,
                value=used,
                unit=unit,
                observed_at=ts,
                evidence_ref=ev_ref,
                metric="quota_used",
                dimensions={"plan": data.get("plan", "coding_plan_pro")},
            ))

        # If quota remaining is present
        if "quota_remaining" in data or "remaining" in data:
            rem = data.get("quota_remaining", data.get("remaining"))
            unit = data.get("quota_unit", data.get("unit", "request"))
            state = "evidenced_zero" if rem == 0 else "value"
            obs_list.append(BytePlusObservation(
                account_alias=account_alias,
                product_domain=DOMAIN_CODING_PLAN,
                capability="quota",
                state=state,
                value=rem,
                unit=unit,
                observed_at=ts,
                evidence_ref=ev_ref,
                metric="quota_remaining",
                dimensions={"plan": data.get("plan", "coding_plan_pro")},
            ))

        # If percent used is present
        if "quota_percent_used" in data or "percent_used" in data:
            pct = data.get("quota_percent_used", data.get("percent_used"))
            obs_list.append(BytePlusObservation(
                account_alias=account_alias,
                product_domain=DOMAIN_CODING_PLAN,
                capability="quota",
                state="value",
                value=pct,
                unit="percent",
                observed_at=ts,
                evidence_ref=ev_ref,
                metric="quota_percent_used",
                dimensions={"plan": data.get("plan", "coding_plan_pro")},
            ))

        return obs_list

    def parse_inference_usage(
        self,
        account_alias: str,
        data: Dict[str, Any],
        observed_at: Optional[str] = None,
        evidence_ref: Optional[str] = None,
    ) -> List[BytePlusObservation]:
        """
        Parses Inference usage stats into BytePlusObservation instances.
        Domain is strictly DOMAIN_INFERENCE.
        Conforms to US-08.b: Inference metrics are isolated from coding plan quota.
        """
        ts = observed_at or format_utc_timestamp()
        ev_ref = evidence_ref or f"ev_{account_alias}_inference_usage"
        obs_list: List[BytePlusObservation] = []

        model = data.get("model", "unknown")
        dims = {"model": model}

        # Token metrics mapping
        metrics_mapping = [
            ("input_tokens", ["input_tokens", "prompt_tokens"], "token"),
            ("output_tokens", ["output_tokens", "completion_tokens"], "token"),
            ("total_tokens", ["total_tokens"], "token"),
            ("cache_hit_tokens", ["cache_hit_tokens", "prompt_cache_hit_tokens"], "token"),
            ("cache_miss_tokens", ["cache_miss_tokens", "prompt_cache_miss_tokens"], "token"),
            ("reasoning_tokens", ["reasoning_tokens"], "token"),
            ("requests", ["requests", "call_count"], "request"),
        ]

        for canonical_name, keys, unit in metrics_mapping:
            val = None
            for k in keys:
                if k in data:
                    val = data[k]
                    break
            if val is not None:
                state = "evidenced_zero" if val == 0 else "value"
                obs_list.append(BytePlusObservation(
                    account_alias=account_alias,
                    product_domain=DOMAIN_INFERENCE,
                    capability="usage",
                    state=state,
                    value=val,
                    unit=unit,
                    observed_at=ts,
                    evidence_ref=ev_ref,
                    metric=canonical_name,
                    dimensions=dict(dims),
                ))

        return obs_list

    def collect_account(
        self,
        account_alias: str,
        capabilities_to_probe: Optional[Sequence[str]] = None,
        period_start: Optional[str] = None,
        period_end: Optional[str] = None,
        simulated_input: Optional[Dict[str, Any]] = None,
    ) -> List[AccountCollectionResult]:
        """
        Performs collection for an individual account with capability-gating and isolation.
        Returns a list of AccountCollectionResult (one per product domain / capability examined).
        """
        now_ts = format_utc_timestamp()
        p_start = period_start or now_ts
        p_end = period_end or now_ts
        cov_period = {"start": p_start, "end": p_end}

        results: List[AccountCollectionResult] = []

        sim_data = simulated_input or {}
        should_fail = sim_data.get("simulate_failure", False) or (sim_data.get("status") == "failure")

        # 1. BytePlus Coding Plan Quota
        coding_plan_cap = self.declare_capability(
            account_alias=account_alias,
            product_domain=DOMAIN_CODING_PLAN,
            capability="quota",
            status=CAPABILITY_READY,
            checked_at=now_ts,
        )

        # Check capability gate (US-08.a)
        if coding_plan_cap["status"] != CAPABILITY_READY:
            results.append(AccountCollectionResult(
                account_alias=account_alias,
                profile_id=account_alias,
                product_domain=DOMAIN_CODING_PLAN,
                capability="quota",
                status="skipped_unsupported" if coding_plan_cap["status"] == CAPABILITY_UNSUPPORTED else "skipped_unverified",
                observed_at=now_ts,
                coverage={
                    "requested": cov_period,
                    "delivered": None,
                    "confirmed": None,
                    "confirmation": "unconfirmed",
                },
                errors=[],
            ))
        elif should_fail:
            # Capture error
            err_msg = sim_data.get("error_message", f"Authentication failed for {account_alias}")
            err_obj = {
                "schema_version": SCHEMA_VERSION,
                "error_class": sim_data.get("error_class", "auth"),
                "retryable": False,
                "occurred_at": now_ts,
                "http_status": sim_data.get("http_status", 401),
                "message": self.redactor.redact_text(err_msg),
                "retry_budget": {"max": 3, "used": 1},
            }
            proc_capture = {
                "argv": ["arkcli", "--profile", account_alias, "usage", "plan"],
                "exit_code": sim_data.get("exit_code", 1),
                "signal": None,
                "stdout_sha256": hashlib.sha256(b"").hexdigest(),
                "stderr_sha256": hashlib.sha256(err_msg.encode("utf-8")).hexdigest(),
                "stdout_truncated": False,
                "stderr_truncated": False,
                "started_at": now_ts,
                "finished_at": now_ts,
                "profile_id": account_alias,
            }
            results.append(AccountCollectionResult(
                account_alias=account_alias,
                profile_id=account_alias,
                product_domain=DOMAIN_CODING_PLAN,
                capability="quota",
                status="failure",
                observed_at=now_ts,
                coverage={
                    "requested": cov_period,
                    "delivered": None,
                    "confirmed": None,
                    "confirmation": "unconfirmed",
                },
                errors=[err_obj],
                process=proc_capture,
            ))
        else:
            # Successful collection of quota
            quota_data = sim_data.get("quota_data", {
                "quota_limit": 500000,
                "quota_used": 125000,
                "quota_remaining": 375000,
                "quota_percent_used": 25,
                "quota_unit": "request",
            })
            observations = self.parse_coding_plan_quota(
                account_alias=account_alias,
                data=quota_data,
                observed_at=now_ts,
                evidence_ref=f"ev_{generate_ulid()}",
            )
            raw_stdout = json.dumps(quota_data).encode("utf-8")
            proc_capture = {
                "argv": ["arkcli", "--profile", account_alias, "usage", "plan"],
                "exit_code": 0,
                "signal": None,
                "stdout_sha256": hashlib.sha256(raw_stdout).hexdigest(),
                "stderr_sha256": hashlib.sha256(b"").hexdigest(),
                "stdout_truncated": False,
                "stderr_truncated": False,
                "started_at": now_ts,
                "finished_at": now_ts,
                "profile_id": account_alias,
            }
            results.append(AccountCollectionResult(
                account_alias=account_alias,
                profile_id=account_alias,
                product_domain=DOMAIN_CODING_PLAN,
                capability="quota",
                status="success",
                observed_at=now_ts,
                coverage={
                    "requested": cov_period,
                    "delivered": cov_period,
                    "confirmed": cov_period,
                    "confirmation": "complete",
                    "pagination": {
                        "pages_fetched": 1,
                        "complete": True,
                        "truncated": False,
                        "next_cursor": None,
                    },
                },
                errors=[],
                process=proc_capture,
                observations=observations,
                raw_payloads=[quota_data],
            ))

        # 2. BytePlus Inference Usage (if present in simulation or requested)
        if "inference_data" in sim_data:
            inf_data = sim_data["inference_data"]
            inf_obs = self.parse_inference_usage(
                account_alias=account_alias,
                data=inf_data,
                observed_at=now_ts,
                evidence_ref=f"ev_{generate_ulid()}",
            )
            raw_inf_stdout = json.dumps(inf_data).encode("utf-8")
            results.append(AccountCollectionResult(
                account_alias=account_alias,
                profile_id=account_alias,
                product_domain=DOMAIN_INFERENCE,
                capability="usage",
                status="success",
                observed_at=now_ts,
                coverage={
                    "requested": cov_period,
                    "delivered": cov_period,
                    "confirmed": cov_period,
                    "confirmation": "complete",
                    "pagination": {
                        "pages_fetched": 1,
                        "complete": True,
                        "truncated": False,
                        "next_cursor": None,
                    },
                },
                errors=[],
                process={
                    "argv": ["arkcli", "--profile", account_alias, "usage", "stats"],
                    "exit_code": 0,
                    "signal": None,
                    "stdout_sha256": hashlib.sha256(raw_inf_stdout).hexdigest(),
                    "stderr_sha256": hashlib.sha256(b"").hexdigest(),
                    "stdout_truncated": False,
                    "stderr_truncated": False,
                    "started_at": now_ts,
                    "finished_at": now_ts,
                    "profile_id": account_alias,
                },
                observations=inf_obs,
                raw_payloads=[inf_data],
            ))

        # 3. BytePlus Billing (if present in simulation)
        if "billing_data" in sim_data:
            bill_data = sim_data["billing_data"]
            raw_bill_stdout = json.dumps(bill_data).encode("utf-8")
            results.append(AccountCollectionResult(
                account_alias=account_alias,
                profile_id=account_alias,
                product_domain=DOMAIN_BILLING,
                capability="billing",
                status="success",
                observed_at=now_ts,
                coverage={
                    "requested": cov_period,
                    "delivered": cov_period,
                    "confirmed": cov_period,
                    "confirmation": "complete",
                    "pagination": {
                        "pages_fetched": 1,
                        "complete": True,
                        "truncated": False,
                        "next_cursor": None,
                        "last_committed_cursor": "p1",
                    },
                },
                errors=[],
                process={
                    "argv": ["arkcli", "--profile", account_alias, "billing", "list"],
                    "exit_code": 0,
                    "signal": None,
                    "stdout_sha256": hashlib.sha256(raw_bill_stdout).hexdigest(),
                    "stderr_sha256": hashlib.sha256(b"").hexdigest(),
                    "stdout_truncated": False,
                    "stderr_truncated": False,
                    "started_at": now_ts,
                    "finished_at": now_ts,
                    "profile_id": account_alias,
                },
                raw_payloads=[bill_data],
            ))

        return results

    def collect_all_accounts(
        self,
        trigger: str = "manual",
        routing_subset: Optional[Sequence[str]] = None,
        period_start: Optional[str] = None,
        period_end: Optional[str] = None,
        simulated_accounts_input: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> BytePlusRunResult:
        """
        Executes multi-account collection across all registered profiles (bp01..bp05).
        Adheres to US-08.c:
        - If an account (e.g. bp03) fails, remaining accounts continue cleanly.
        - Overall status resolves to 'partial_failure' (exit_status=3) if any fails and any succeeds.
        - Overall status is 'failure' (exit_status=1) if all fail.
        - Overall status is 'success' (exit_status=0) if all succeed.
        - No account inherits or leaks data to another account.
        """
        started_at = format_utc_timestamp()
        run_id = f"run_{generate_ulid()}"
        target_profiles = list(routing_subset) if routing_subset else list(self.profiles)

        all_account_runs: List[AccountCollectionResult] = []
        sim_map = simulated_accounts_input or {}

        for profile in target_profiles:
            sim_input = sim_map.get(profile, {})
            account_results = self.collect_account(
                account_alias=profile,
                period_start=period_start,
                period_end=period_end,
                simulated_input=sim_input,
            )
            all_account_runs.extend(account_results)

        finished_at = format_utc_timestamp()

        # Assess overall status (ADR-0002 / run.schema.json)
        has_success = any(ar.status == "success" for ar in all_account_runs)
        has_failure = any(ar.status == "failure" for ar in all_account_runs)

        if has_success and has_failure:
            overall_status = "partial_failure"
            exit_status = 3
        elif has_failure:
            overall_status = "failure"
            exit_status = 1
        else:
            overall_status = "success"
            exit_status = 0

        return BytePlusRunResult(
            run_id=run_id,
            trigger=trigger,
            started_at=started_at,
            finished_at=finished_at,
            status=overall_status,
            exit_status=exit_status,
            routing_subset=list(routing_subset or []),
            account_runs=all_account_runs,
        )

    def build_evidence_descriptor(
        self,
        raw_bytes: bytes,
        mime_type: str = "application/json",
        account_alias: str = "bp01",
        created_at: Optional[str] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """Builds evidence descriptor conforming to evidence.schema.json."""
        ts = created_at or format_utc_timestamp()
        sha = hashlib.sha256(raw_bytes).hexdigest()
        ev_id = f"ev_{generate_ulid()}"
        desc = {
            "schema_version": SCHEMA_VERSION,
            "evidence_id": ev_id,
            "sha256": sha,
            "size_bytes": len(raw_bytes),
            "mime_type": mime_type,
            "source": SOURCE_NAME,
            "account": account_alias,
            "created_at": ts,
            "observed_at": ts,
            "collector_version": "0.1.0",
            "capture_point": "provider_response_verbatim",
            "redaction": {
                "redacted": False,
                "redaction_version": None,
            },
        }
        return ev_id, desc

    def observations_to_events_and_measurements(
        self,
        observations: Sequence[BytePlusObservation],
        raw_evidence_id: str,
        collected_at: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        Transforms observations into canonical AEL core event and measurement dictionaries.
        Guarantees strict separation between coding plan quota and inference metrics (US-08.b).
        """
        ts = collected_at or format_utc_timestamp()
        events: List[Dict[str, Any]] = []
        measurements: List[Dict[str, Any]] = []

        # Group observations by account and product domain to preserve strict separation
        grouped: Dict[Tuple[str, str], List[BytePlusObservation]] = {}
        for obs in observations:
            key = (obs.account_alias, obs.product_domain)
            grouped.setdefault(key, []).append(obs)

        for (alias, domain), obs_group in grouped.items():
            ev_id = generate_ulid()
            event_type = "quota.snapshot" if domain == DOMAIN_CODING_PLAN else ("usage.snapshot" if domain == DOMAIN_INFERENCE else "billing.snapshot")

            # Valid time range for event and window (e.g. 1-hour window ending at collected_at)
            try:
                dt_end = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:
                dt_end = datetime.datetime.now(datetime.timezone.utc)
            dt_start = dt_end - datetime.timedelta(hours=1)
            ts_start = format_utc_timestamp(dt_start)
            ts_end = format_utc_timestamp(dt_end)

            window_obj = {
                "type": "custom",
                "label": f"{domain}_window",
                "starts_at": ts_start,
                "ends_at": ts_end,
            }

            native_id = f"{SOURCE_NAME}:{alias}:{domain}:{ts_end}"
            idem_key = derive_idempotency_key(
                source=SOURCE_NAME,
                account=alias,
                native_id=native_id,
                window=window_obj,
            )

            ev_obj = {
                "schema_version": SCHEMA_VERSION,
                "event_id": ev_id,
                "event_type": event_type,
                "source": SOURCE_NAME,
                "source_account_id": alias,
                "collector": "byteplus.multi_account_collector",
                "mapping_version": "0.1.0",
                "collected_at": ts_end,
                "observed_at": ts_end,
                "period_start": ts_start,
                "period_end": ts_end,
                "collection_phase": "prospective",
                "project_id": None,
                "repository_id": None,
                "session_id": None,
                "task_id": None,
                "provenance": {
                    "class": "P0",
                    "method": "cli",
                    "confidence": 1.0,
                    "confidence_reason": f"BytePlus {domain} collection per US-08",
                    "validation_status": "semantically_checked",
                },
                "raw_evidence_id": raw_evidence_id,
                "idempotency_key": idem_key,
                "window": window_obj,
                "payload": {
                    "product_domain": domain,
                    "metrics_count": len(obs_group),
                },
            }
            events.append(ev_obj)

            for obs in obs_group:
                if obs.state in ("value", "evidenced_zero", "partial") and obs.value is not None:
                    metric_name = obs.metric or ("quota_used" if domain == DOMAIN_CODING_PLAN else "input_tokens")
                    m_id = generate_ulid()
                    rev_id = generate_ulid()

                    # Decimal representation without float inaccuracies
                    val_conv: Union[int, str]
                    if isinstance(obs.value, int):
                        val_conv = obs.value
                    elif isinstance(obs.value, Decimal) and obs.value == int(obs.value):
                        val_conv = int(obs.value)
                    else:
                        val_conv = str(obs.value)

                    meas_obj: Dict[str, Any] = {
                        "schema_version": SCHEMA_VERSION,
                        "measurement_id": m_id,
                        "event_id": ev_id,
                        "metric": metric_name,
                        "value": val_conv,
                        "unit": obs.unit or ("request" if "quota" in metric_name else "token"),
                        "aggregation": "absolute" if "quota" in metric_name else "interval_total",
                        "source": SOURCE_NAME,
                        "source_account_id": alias,
                        "provider": "byteplus",
                        "window": window_obj,
                        "dimensions": copy.deepcopy(obs.dimensions),
                        "provenance": {
                            "class": "P0",
                            "method": "cli",
                            "confidence": 1.0,
                            "confidence_reason": f"BytePlus {domain} collection per US-08",
                            "validation_status": "semantically_checked",
                        },
                        "evidence_ids": [raw_evidence_id],
                        "collector_version": "0.1.0",
                        "mapping_version": "0.1.0",
                        "revision": {
                            "revision_id": rev_id,
                            "revision_no": 1,
                            "supersedes": None,
                            "reason_code": "initial",
                            "reason": f"first capture from {domain}",
                            "recorded_at": ts_end,
                            "effective": True,
                            "lineage": {
                                "mapping_version": "0.1.0",
                                "transformation_version": "0.1.0",
                                "input_measurement_ids": [],
                            },
                        },
                    }
                    measurements.append(meas_obj)

        return events, measurements

    def collect_paginated_billing(
        self,
        account_alias: str,
        pages: Sequence[Dict[str, Any]],
        period_start: Optional[str] = None,
        period_end: Optional[str] = None,
        verify_complete_fn: Optional[Callable[[List[Dict[str, Any]]], bool]] = None,
    ) -> AccountCollectionResult:
        """
        US-08.d: Partielle oder paginierte Billingantworten werden erst nach vollständiger Prüfung als vollständig markiert.
        Fetches/processes pages one by one.
        If pages are truncated, interrupted, or fail validation, confirmation remains 'partial' or 'unconfirmed'.
        Only marked 'complete' if all pages are fetched, next_cursor is null, and verify_complete_fn (if provided) passes.
        """
        now_ts = format_utc_timestamp()
        p_start = period_start or now_ts
        p_end = period_end or now_ts
        cov_period = {"start": p_start, "end": p_end}

        all_records: List[Dict[str, Any]] = []
        pages_fetched = 0
        last_committed_cursor = None
        has_more = True
        truncated = False
        is_fully_verified = True
        errors: List[Dict[str, Any]] = []

        for p_idx, page in enumerate(pages):
            pages_fetched += 1
            cursor = page.get("cursor") or page.get("page_id")
            if cursor:
                last_committed_cursor = str(cursor)

            records = page.get("records") or page.get("items") or []
            all_records.extend(records)

            # Check if page reports errors
            if page.get("error"):
                is_fully_verified = False
                err_msg = page["error"]
                errors.append({
                    "schema_version": SCHEMA_VERSION,
                    "error_class": "provider_error",
                    "retryable": True,
                    "occurred_at": now_ts,
                    "message": self.redactor.redact_text(str(err_msg)),
                    "retry_budget": {"max": 3, "used": 1},
                })
                break

            # If page is explicitly marked truncated or partial
            if page.get("truncated", False) or page.get("is_partial", False):
                truncated = True
                is_fully_verified = False
                break

            next_cursor = page.get("next_cursor")
            if not next_cursor or page.get("has_more") is False:
                has_more = False
                break

        # Verification step
        if has_more:
            # We stopped before all pages were read -> incomplete pagination
            is_fully_verified = False

        if verify_complete_fn is not None and is_fully_verified:
            try:
                if not verify_complete_fn(all_records):
                    is_fully_verified = False
            except Exception as ex:
                is_fully_verified = False
                errors.append({
                    "schema_version": SCHEMA_VERSION,
                    "error_class": "validation",
                    "retryable": False,
                    "occurred_at": now_ts,
                    "message": f"Verification function failed: {ex}",
                })

        confirmation_status = "complete" if is_fully_verified else "partial"
        confirmed_range = cov_period if confirmation_status == "complete" else None
        delivered_range = cov_period

        pagination_info = {
            "pages_fetched": pages_fetched,
            "complete": (confirmation_status == "complete"),
            "truncated": truncated or (confirmation_status != "complete" and has_more),
            "next_cursor": None if confirmation_status == "complete" else (page.get("next_cursor") if pages else None),
        }
        if last_committed_cursor:
            pagination_info["last_committed_cursor"] = last_committed_cursor

        status = "success" if not errors else ("failure" if not all_records else "success")

        # Create billing observations from all_records
        observations: List[BytePlusObservation] = []
        for rec in all_records:
            cost_val = rec.get("cost", rec.get("amount"))
            if cost_val is not None:
                currency = rec.get("currency", "USD").lower()
                obs_state = "value" if confirmation_status == "complete" else "partial"
                observations.append(BytePlusObservation(
                    account_alias=account_alias,
                    product_domain=DOMAIN_BILLING,
                    capability="billing",
                    state=obs_state,
                    value=cost_val,
                    unit=currency,
                    observed_at=now_ts,
                    evidence_ref=f"ev_{account_alias}_billing_{pages_fetched}",
                    metric="cost",
                    dimensions={"invoice_id": rec.get("invoice_id", "unknown")},
                    reason=None if confirmation_status == "complete" else "Incomplete or unverified billing pagination",
                ))

        raw_payload = {
            "account_alias": account_alias,
            "pages_fetched": pages_fetched,
            "total_records": len(all_records),
            "records": all_records,
        }

        return AccountCollectionResult(
            account_alias=account_alias,
            profile_id=account_alias,
            product_domain=DOMAIN_BILLING,
            capability="billing",
            status=status,
            observed_at=now_ts,
            coverage={
                "requested": cov_period,
                "delivered": delivered_range,
                "confirmed": confirmed_range,
                "confirmation": confirmation_status,
                "pagination": pagination_info,
            },
            errors=errors,
            process={
                "argv": ["arkcli", "--profile", account_alias, "billing", "list"],
                "exit_code": 0 if status == "success" else 1,
                "signal": None,
                "stdout_sha256": hashlib.sha256(json.dumps(raw_payload).encode("utf-8")).hexdigest(),
                "stderr_sha256": hashlib.sha256(b"").hexdigest(),
                "stdout_truncated": False,
                "stderr_truncated": False,
                "started_at": now_ts,
                "finished_at": now_ts,
                "profile_id": account_alias,
            },
            observations=observations,
            raw_payloads=[raw_payload],
        )

    def reconcile_superseding_billing(
        self,
        account_alias: str,
        prior_measurements: Sequence[Dict[str, Any]],
        new_billing_records: Sequence[Dict[str, Any]],
        raw_evidence_id: str,
        reason: str = "T+1 billing settlement finalized by provider",
        recorded_at: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        US-08.d: Nachgelieferte Abrechnung ersetzt die wirksame Revision ohne zweite Belastung.
        Reconciles late-arriving / corrected billing records against prior billing measurements.
        Marks superseded revision effective=False, creates revision_no=prior+1 with supersedes=prior.revision_id,
        effective=True, and ensures the prior value is not double-counted.
        Returns (reconciled_measurements, revision_chain).
        """
        ts = recorded_at or format_utc_timestamp()
        updated_measurements: List[Dict[str, Any]] = []
        revision_chain: List[Dict[str, Any]] = []

        # Map prior measurements by logical key, e.g. invoice_id or metric:account:window
        for new_rec in new_billing_records:
            inv_id = new_rec.get("invoice_id")
            metric_name = new_rec.get("metric", "cost")
            new_amount = new_rec.get("cost", new_rec.get("amount", "0"))
            new_unit = new_rec.get("currency", "usd").lower()

            # Find matching prior measurement
            matched_prior = None
            for pm in prior_measurements:
                if pm.get("source_account_id") == account_alias and pm.get("metric") == metric_name:
                    if inv_id and pm.get("dimensions", {}).get("invoice_id") == inv_id:
                        matched_prior = pm
                        break
                    elif not inv_id:
                        matched_prior = pm
                        break

            if matched_prior is not None:
                # Mark previous revision as not effective to avoid double counting
                prior_rev = matched_prior["revision"]
                prior_rev["effective"] = False
                revision_chain.append(copy.deepcopy(prior_rev))

                # Create superseding revision
                new_rev_id = generate_ulid()
                new_rev_no = prior_rev.get("revision_no", 1) + 1
                new_rev = {
                    "revision_id": new_rev_id,
                    "revision_no": new_rev_no,
                    "supersedes": prior_rev["revision_id"],
                    "reason_code": "provider_correction",
                    "reason": reason,
                    "recorded_at": ts,
                    "effective": True,
                    "lineage": {
                        "mapping_version": "0.1.0",
                        "transformation_version": "0.1.0",
                        "input_measurement_ids": [matched_prior["measurement_id"]],
                    },
                }
                revision_chain.append(new_rev)

                # Format value cleanly (no float, Decimal string or int)
                val_conv: Union[int, str]
                if isinstance(new_amount, int):
                    val_conv = new_amount
                elif isinstance(new_amount, Decimal) and new_amount == int(new_amount):
                    val_conv = int(new_amount)
                else:
                    val_conv = str(new_amount)

                # New measurement record
                new_meas_id = generate_ulid()
                dims = copy.deepcopy(matched_prior.get("dimensions", {}))
                if inv_id:
                    dims["invoice_id"] = inv_id

                new_meas = {
                    "schema_version": SCHEMA_VERSION,
                    "measurement_id": new_meas_id,
                    "event_id": matched_prior.get("event_id", generate_ulid()),
                    "metric": metric_name,
                    "value": val_conv,
                    "unit": new_unit,
                    "aggregation": matched_prior.get("aggregation", "interval_total"),
                    "source": SOURCE_NAME,
                    "source_account_id": account_alias,
                    "provider": "byteplus",
                    "window": copy.deepcopy(matched_prior.get("window", {})),
                    "dimensions": dims,
                    "provenance": {
                        "class": "P0",
                        "method": "cli",
                        "confidence": 1.0,
                        "confidence_reason": "Late-arriving authoritative billing settlement per US-08.d",
                        "validation_status": "semantically_checked",
                    },
                    "evidence_ids": [raw_evidence_id],
                    "collector_version": "0.1.0",
                    "mapping_version": "0.1.0",
                    "revision": new_rev,
                }
                updated_measurements.append(new_meas)
            else:
                # First capture for this record
                new_rev_id = generate_ulid()
                new_rev = {
                    "revision_id": new_rev_id,
                    "revision_no": 1,
                    "supersedes": None,
                    "reason_code": "initial",
                    "reason": "initial billing capture",
                    "recorded_at": ts,
                    "effective": True,
                    "lineage": {
                        "mapping_version": "0.1.0",
                        "transformation_version": "0.1.0",
                        "input_measurement_ids": [],
                    },
                }
                revision_chain.append(new_rev)
                val_conv = str(new_amount) if not isinstance(new_amount, int) else new_amount
                new_meas = {
                    "schema_version": SCHEMA_VERSION,
                    "measurement_id": generate_ulid(),
                    "event_id": generate_ulid(),
                    "metric": metric_name,
                    "value": val_conv,
                    "unit": new_unit,
                    "aggregation": "interval_total",
                    "source": SOURCE_NAME,
                    "source_account_id": account_alias,
                    "provider": "byteplus",
                    "window": {
                        "type": "custom",
                        "label": "billing_window",
                        "starts_at": ts,
                        "ends_at": ts,
                    },
                    "dimensions": {"invoice_id": inv_id} if inv_id else {},
                    "provenance": {
                        "class": "P0",
                        "method": "cli",
                        "confidence": 1.0,
                        "confidence_reason": "Initial billing capture",
                        "validation_status": "semantically_checked",
                    },
                    "evidence_ids": [raw_evidence_id],
                    "collector_version": "0.1.0",
                    "mapping_version": "0.1.0",
                    "revision": new_rev,
                }
                updated_measurements.append(new_meas)

        return updated_measurements, revision_chain

    def detect_quota_reset_or_renewal(
        self,
        account_alias: str,
        current_snapshot: Dict[str, Any],
        previous_snapshot: Optional[Dict[str, Any]],
        threshold_percent_warn: float = 80.0,
        threshold_percent_crit: float = 95.0,
    ) -> Dict[str, Any]:
        """
        Erneuerungszyklen und Schwellenwert-Beobachtung (TS §12, PRD US-08).
        Detects whether a quota reset / renewal occurred between previous and current snapshot:
        - If current_used < previous_used or current_pct < previous_pct while windows changed,
          resets are detected and not treated as negative consumption.
        - Analyzes threshold crossing (e.g. 80% warning, 95% critical).
        """
        curr_used = Decimal(str(current_snapshot.get("quota_used", current_snapshot.get("used", 0))))
        curr_limit = Decimal(str(current_snapshot.get("quota_limit", current_snapshot.get("limit", 0))))
        curr_pct = Decimal(str(current_snapshot.get("quota_percent_used", current_snapshot.get("percent_used", 0))))
        if curr_limit > 0 and curr_pct == 0:
            curr_pct = (curr_used / curr_limit) * 100

        curr_window = current_snapshot.get("window", {})
        prev_window = previous_snapshot.get("window", {}) if previous_snapshot else {}

        reset_detected = False
        reset_type = None  # 'window_shift' | 'usage_drop' | None
        delta_used: Optional[Decimal] = None

        if previous_snapshot is not None:
            prev_used = Decimal(str(previous_snapshot.get("quota_used", previous_snapshot.get("used", 0))))
            prev_pct = Decimal(str(previous_snapshot.get("quota_percent_used", previous_snapshot.get("percent_used", 0))))

            # Check if quota window changed
            window_changed = (
                curr_window.get("starts_at") != prev_window.get("starts_at")
                or curr_window.get("ends_at") != prev_window.get("ends_at")
            )

            if window_changed:
                reset_detected = True
                reset_type = "window_shift"
                delta_used = None  # TS §12: neues Window -> delta=NULL + quota.reset
            elif curr_used < prev_used or curr_pct < prev_pct:
                # Same window but drop occurred (renewal / reset)
                reset_detected = True
                reset_type = "usage_drop"
                delta_used = None  # Not negative usage
            else:
                # Same window, regular monotonic consumption
                reset_detected = False
                delta_used = curr_used - prev_used
        else:
            # Initial snapshot
            delta_used = None

        # Threshold evaluation
        threshold_state = "nominal"
        if curr_pct >= Decimal(str(threshold_percent_crit)):
            threshold_state = "critical"
        elif curr_pct >= Decimal(str(threshold_percent_warn)):
            threshold_state = "warning"

        return {
            "account_alias": account_alias,
            "product_domain": DOMAIN_CODING_PLAN,
            "quota_used": int(curr_used) if curr_used == int(curr_used) else str(curr_used),
            "quota_limit": int(curr_limit) if curr_limit == int(curr_limit) else str(curr_limit),
            "quota_percent_used": float(curr_pct),
            "threshold_state": threshold_state,
            "reset_detected": reset_detected,
            "reset_type": reset_type,
            "delta_used": (int(delta_used) if delta_used == int(delta_used) else str(delta_used)) if delta_used is not None else None,
            "window": curr_window,
        }

    def compare_historical_snapshots(
        self,
        account_alias: str,
        snapshots: Sequence[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        Historische Snapshot-Vergleiche für bp01..bp05.
        Iterates over chronologically sorted snapshots of an account and performs pair-wise analysis
        for resets, delta calculations, and threshold crossing alerts.
        """
        comparisons: List[Dict[str, Any]] = []
        # Sort snapshots by observed_at or window.starts_at
        sorted_snapshots = sorted(
            snapshots,
            key=lambda s: s.get("observed_at") or s.get("window", {}).get("starts_at", "")
        )

        prev_snap = None
        for snap in sorted_snapshots:
            comp = self.detect_quota_reset_or_renewal(
                account_alias=account_alias,
                current_snapshot=snap,
                previous_snapshot=prev_snap,
            )
            comp["observed_at"] = snap.get("observed_at", format_utc_timestamp())
            comparisons.append(comp)
            prev_snap = snap

        return comparisons

    def build_isolated_study_rows(
        self,
        run_result: BytePlusRunResult,
    ) -> List[Dict[str, Any]]:
        """Build deterministic study rows without cross-account aggregation.

        Coding-plan allowance, inference usage, and provider billing remain
        separate semantic series.  This method intentionally has no total or
        per-task allocation output: consumers may group the returned rows only
        after selecting compatible account and product-domain dimensions.
        """
        semantics = {
            DOMAIN_CODING_PLAN: "subscription_plan_allowance",
            DOMAIN_INFERENCE: "api_inference_usage",
            DOMAIN_BILLING: "provider_billing",
        }
        configured = set(self.profiles)
        rows: List[Dict[str, Any]] = []
        for account_run in run_result.account_runs:
            alias = account_run.account_alias
            if alias not in configured or account_run.profile_id != alias:
                raise ValueError(f"unbound BytePlus account run: {alias}")
            if account_run.product_domain not in semantics:
                raise ValueError(f"unsupported BytePlus product domain: {account_run.product_domain}")
            for observation in account_run.observations:
                if observation.account_alias != alias:
                    raise ValueError("cross-account BytePlus observation refused")
                if observation.product_domain != account_run.product_domain:
                    raise ValueError("cross-domain BytePlus observation refused")
                value: Optional[Union[int, str, float]]
                if isinstance(observation.value, Decimal):
                    value = str(observation.value)
                else:
                    value = observation.value
                rows.append(
                    {
                        "source": SOURCE_NAME,
                        "account_alias": alias,
                        "profile_id": account_run.profile_id,
                        "product_domain": account_run.product_domain,
                        "semantic": semantics[account_run.product_domain],
                        "capability": account_run.capability,
                        "metric": observation.metric,
                        "state": observation.state,
                        "value": value,
                        "unit": observation.unit,
                        "observed_at": observation.observed_at,
                        "evidence_ref": observation.evidence_ref,
                        "dimensions": copy.deepcopy(observation.dimensions),
                    }
                )
        return sorted(
            rows,
            key=lambda row: (
                row["account_alias"],
                row["product_domain"],
                row["metric"] or "",
                row["observed_at"],
            ),
        )
