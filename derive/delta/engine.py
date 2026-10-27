from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable


@dataclass(frozen=True)
class Snapshot:
    measurement_id: str
    observed_at: str
    value: int | str
    source: str
    account: str
    product: str
    metric: str
    unit: str
    window_id: str | None
    window_kind: str
    definition_version: str
    dimensions: dict[str, str] = field(default_factory=dict)
    limit: int | str | None = None


def _compatible(a: Snapshot, b: Snapshot) -> bool:
    return (a.source, a.account, a.product, a.metric, a.unit, a.dimensions, a.definition_version) == (
        b.source, b.account, b.product, b.metric, b.unit, b.dimensions, b.definition_version
    )


def derive_series(snapshots: Iterable[Snapshot]) -> list[dict[str, Any]]:
    ordered = sorted(snapshots, key=lambda x: (x.observed_at, x.measurement_id))
    result: list[dict[str, Any]] = []
    previous: Snapshot | None = None
    for current in ordered:
        status, delta, semantic = "first", None, "not_available"
        reason = "first snapshot"
        if previous is not None:
            if not _compatible(previous, current):
                status, reason = "not_comparable", "scope or definition changed"
            elif previous.limit != current.limit:
                status, reason = "not_comparable", "budget_or_limit_changed"
            elif previous.window_id != current.window_id and current.window_id is not None:
                status, reason = "reset", "evidenced_window_change"
            else:
                change = Decimal(str(current.value)) - Decimal(str(previous.value))
                if current.window_kind == "rolling":
                    delta, semantic, status, reason = str(change), "net_change", "derived", "rolling window"
                elif change < 0:
                    status, reason = "unclear", "decrease_without_evidenced_window_change"
                else:
                    delta, semantic, status, reason = str(change), "usage_delta", "derived", "compatible snapshots"
        version_payload = {
            "current": current.measurement_id,
            "previous": previous.measurement_id if previous else None,
            "status": status, "delta": delta, "semantic": semantic,
        }
        result.append({
            **version_payload,
            "reason": reason,
            "derivation_version": hashlib.sha256(
                json.dumps(version_payload, sort_keys=True).encode()
            ).hexdigest(),
            "observed_at": current.observed_at,
        })
        previous = current
    return result
