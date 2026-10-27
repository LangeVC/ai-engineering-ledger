from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class ComparableScope:
    interval_start: str
    interval_end: str
    account: str
    product: str
    metric_definition: str
    aggregation: str
    routing_subset: tuple[str, ...]


def compare(provider: dict[str, Any] | None, runtime: dict[str, Any], provider_scope: ComparableScope | None,
            runtime_scope: ComparableScope, previous_revision: str | None = None) -> dict[str, Any]:
    if provider is None or provider_scope is None:
        payload = {"status": "provider_missing", "reason": "runtime is not provider truth",
                   "provider_value": None, "runtime_value": str(runtime["value"])}
    elif provider_scope != runtime_scope:
        mismatches = [k for k, v in asdict(provider_scope).items() if v != asdict(runtime_scope)[k]]
        payload = {"status": "not_comparable", "reason": "scope_mismatch", "mismatches": mismatches,
                   "provider_value": str(provider["value"]), "runtime_value": str(runtime["value"])}
    else:
        p, r = Decimal(str(provider["value"])), Decimal(str(runtime["value"]))
        delta = r - p
        if p == 0:
            status = "no_usage" if r == 0 else "deviation_undefined"
            coverage = None
        else:
            coverage = r / p * Decimal("100")
            status = "over_coverage_review" if coverage > 100 else "comparable"
        payload = {"status": status, "provider_value": str(p), "runtime_value": str(r),
                   "delta": str(delta), "coverage_percent": str(coverage) if coverage is not None else None,
                   "accuracy_confirmed": False}
    identity = {
        "payload": payload, "provider_revision": provider.get("revision") if provider else None,
        "runtime_revision": runtime.get("revision"), "scope": asdict(runtime_scope),
    }
    revision = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return {**payload, "comparison_revision": revision, "supersedes": previous_revision}
