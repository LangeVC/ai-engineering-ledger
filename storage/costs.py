"""Versioned period-cost ledger for AEL study datasets.

The ledger preserves cash-flow versus consumption semantics and never invents
per-task allocation.  Allocations exist only when supplied explicitly with
their own evidence references; the remainder is always visible.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence


COST_KINDS = {"subscription", "api_usage", "infrastructure", "credit_purchase", "other"}
COST_SEMANTICS = {"cashflow", "consumption"}


def _decimal(value: Decimal | str | int) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("cost amount must be a decimal") from exc
    if not result.is_finite() or result < 0:
        raise ValueError("cost amount must be finite and non-negative")
    return result


def _utc(value: str) -> str:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("cost periods require timezone-aware timestamps")
    return parsed.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


@dataclasses.dataclass(frozen=True)
class CostAllocation:
    subject_id: str
    amount: Decimal
    evidence_refs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject_id": self.subject_id,
            "amount": str(self.amount),
            "evidence_refs": list(self.evidence_refs),
        }


@dataclasses.dataclass(frozen=True)
class PeriodCost:
    cost_id: str
    source: str
    account_alias: str
    period_start: str
    period_end: str
    amount: Decimal
    currency: str
    cost_kind: str
    semantic: str
    evidence_refs: tuple[str, ...]
    allocations: tuple[CostAllocation, ...]
    unallocated_remainder: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "0.3",
            "cost_id": self.cost_id,
            "source": self.source,
            "account_alias": self.account_alias,
            "period": {"start": self.period_start, "end": self.period_end, "boundary": "half_open"},
            "amount": str(self.amount),
            "currency": self.currency,
            "cost_kind": self.cost_kind,
            "semantic": self.semantic,
            "evidence_refs": list(self.evidence_refs),
            "allocations": [allocation.to_dict() for allocation in self.allocations],
            "unallocated_remainder": str(self.unallocated_remainder),
        }


class CostLedger:
    """In-memory deterministic ledger used by storage/export layers."""

    def __init__(self) -> None:
        self._records: dict[str, PeriodCost] = {}

    def add_period_cost(
        self,
        *,
        source: str,
        account_alias: str,
        period_start: str,
        period_end: str,
        amount: Decimal | str | int,
        currency: str,
        cost_kind: str,
        semantic: str,
        evidence_refs: Sequence[str],
        allocations: Mapping[str, Decimal | str | int] | None = None,
        allocation_evidence: Mapping[str, Sequence[str]] | None = None,
    ) -> PeriodCost:
        start = _utc(period_start)
        end = _utc(period_end)
        if start >= end:
            raise ValueError("cost period must be a non-empty half-open interval")
        normalized_amount = _decimal(amount)
        normalized_currency = currency.strip().upper()
        if len(normalized_currency) != 3 or not normalized_currency.isalpha():
            raise ValueError("currency must be a three-letter code")
        if cost_kind not in COST_KINDS:
            raise ValueError(f"unsupported cost_kind: {cost_kind}")
        if semantic not in COST_SEMANTICS:
            raise ValueError(f"unsupported cost semantic: {semantic}")
        evidence = tuple(sorted({str(ref).strip() for ref in evidence_refs if str(ref).strip()}))
        if not evidence:
            raise ValueError("period cost requires at least one evidence reference")

        explicit_allocations: list[CostAllocation] = []
        allocated = Decimal("0")
        for subject_id, raw_value in sorted((allocations or {}).items()):
            value = _decimal(raw_value)
            refs = tuple(
                sorted(
                    {
                        str(ref).strip()
                        for ref in (allocation_evidence or {}).get(subject_id, ())
                        if str(ref).strip()
                    }
                )
            )
            if not refs:
                raise ValueError(f"allocation {subject_id} requires explicit evidence")
            explicit_allocations.append(CostAllocation(subject_id, value, refs))
            allocated += value
        if allocated > normalized_amount:
            raise ValueError("explicit allocations exceed period cost")

        identity = {
            "source": source,
            "account_alias": account_alias,
            "period_start": start,
            "period_end": end,
            "amount": str(normalized_amount),
            "currency": normalized_currency,
            "cost_kind": cost_kind,
            "semantic": semantic,
            "evidence_refs": evidence,
            "allocations": [item.to_dict() for item in explicit_allocations],
        }
        digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        record = PeriodCost(
            cost_id=f"cost_{digest[:24]}",
            source=source,
            account_alias=account_alias,
            period_start=start,
            period_end=end,
            amount=normalized_amount,
            currency=normalized_currency,
            cost_kind=cost_kind,
            semantic=semantic,
            evidence_refs=evidence,
            allocations=tuple(explicit_allocations),
            unallocated_remainder=normalized_amount - allocated,
        )
        self._records.setdefault(record.cost_id, record)
        return self._records[record.cost_id]

    def export(self) -> list[dict[str, Any]]:
        """Return content-stable records without currency/semantic aggregation."""
        return [self._records[key].to_dict() for key in sorted(self._records)]
