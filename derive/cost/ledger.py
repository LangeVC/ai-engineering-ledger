from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

KINDS = {"subscription_fee", "api_usage_measured", "api_usage_estimated",
         "credit_purchase", "credit_consumption", "refund", "unknown_fee"}


@dataclass(frozen=True)
class CostEvent:
    event_id: str
    kind: str
    amount: str
    currency: str
    entitlement_id: str | None = None
    billing_period: str | None = None
    fx_rate: str | None = None
    fx_date: str | None = None
    fx_source: str | None = None
    fx_target_currency: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError("unsupported cost kind")
        Decimal(self.amount)
        if len(self.currency) != 3:
            raise ValueError("currency must be ISO-4217 style")
        supplied = (self.fx_rate, self.fx_date, self.fx_source, self.fx_target_currency)
        if any(supplied) and not all(supplied):
            raise ValueError("FX conversion requires rate, date, and source")


def aggregate_costs(events: Iterable[CostEvent]) -> dict:
    unique: dict[str, CostEvent] = {}
    entitlement_subscriptions: set[tuple[str, str, str, str | None]] = set()
    for event in events:
        key = event.event_id
        if event.kind == "subscription_fee" and event.entitlement_id:
            subscription_key = (event.entitlement_id, event.amount, event.currency, event.billing_period)
            if subscription_key in entitlement_subscriptions:
                continue
            entitlement_subscriptions.add(subscription_key)
        unique.setdefault(key, event)
    categories: dict[str, dict[str, Decimal]] = {}
    cashflow: dict[str, Decimal] = {}
    consumption: dict[str, Decimal] = {}
    unknown_currencies: set[str] = set()
    conversions = []
    for event in unique.values():
        amount = Decimal(event.amount)
        categories.setdefault(event.kind, {}).setdefault(event.currency, Decimal("0"))
        categories[event.kind][event.currency] += amount
        if event.kind in {"subscription_fee", "api_usage_measured", "credit_purchase", "refund", "unknown_fee"}:
            cashflow[event.currency] = cashflow.get(event.currency, Decimal("0")) + amount
        if event.kind in {"api_usage_measured", "api_usage_estimated", "credit_consumption"}:
            consumption[event.currency] = consumption.get(event.currency, Decimal("0")) + amount
        if event.kind == "unknown_fee":
            unknown_currencies.add(event.currency)
        if event.fx_rate:
            converted = amount * Decimal(event.fx_rate)
            conversions.append({
                "event_id": event.event_id, "from_currency": event.currency,
                "to_currency": event.fx_target_currency, "source_amount": format(amount, "f"),
                "rate": event.fx_rate, "converted_amount": format(converted, "f"),
                "rate_date": event.fx_date, "rate_source": event.fx_source,
                "rule": "source_amount_times_rate",
            })
    fmt = lambda values: {k: format(v, "f") for k, v in sorted(values.items())}
    return {
        "categories": {k: fmt(v) for k, v in sorted(categories.items())},
        "cashflow_view": fmt(cashflow),
        "consumption_view": fmt(consumption),
        "standard_total_rule": "cashflow_and_consumption_are_separate",
        "unknown_not_free": sorted(unknown_currencies),
        "fx_conversions": conversions,
    }
