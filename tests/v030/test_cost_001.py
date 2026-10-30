from __future__ import annotations

from decimal import Decimal

import pytest

from storage.costs import CostLedger


def test_period_cost_preserves_currency_kind_semantic_and_evidence() -> None:
    ledger = CostLedger()
    record = ledger.add_period_cost(
        source="anthropic",
        account_alias="claude-pro",
        period_start="2026-10-01T00:00:00+02:00",
        period_end="2026-11-01T00:00:00+01:00",
        amount="20.00",
        currency="eur",
        cost_kind="subscription",
        semantic="cashflow",
        evidence_refs=["ev_invoice_october"],
    )

    assert record.currency == "EUR"
    assert record.cost_kind == "subscription"
    assert record.semantic == "cashflow"
    assert record.evidence_refs == ("ev_invoice_october",)
    assert record.allocations == ()
    assert record.unallocated_remainder == Decimal("20.00")


def test_allocation_is_never_fabricated_and_requires_evidence() -> None:
    ledger = CostLedger()
    with pytest.raises(ValueError, match="requires explicit evidence"):
        ledger.add_period_cost(
            source="byteplus",
            account_alias="byteplus-01",
            period_start="2026-10-01T00:00:00Z",
            period_end="2026-11-01T00:00:00Z",
            amount="10",
            currency="USD",
            cost_kind="subscription",
            semantic="cashflow",
            evidence_refs=["ev_plan"],
            allocations={"task-1": "3"},
        )

    record = ledger.add_period_cost(
        source="byteplus",
        account_alias="byteplus-01",
        period_start="2026-10-01T00:00:00Z",
        period_end="2026-11-01T00:00:00Z",
        amount="10",
        currency="USD",
        cost_kind="subscription",
        semantic="cashflow",
        evidence_refs=["ev_plan"],
        allocations={"repo:ael": "3"},
        allocation_evidence={"repo:ael": ["ev_mapping"]},
    )
    assert record.unallocated_remainder == Decimal("7")
    assert record.allocations[0].subject_id == "repo:ael"


def test_export_is_deterministic_and_does_not_mix_cashflow_with_consumption() -> None:
    ledger = CostLedger()
    common = {
        "source": "deepseek",
        "account_alias": "deepseek-main",
        "period_start": "2026-10-01T00:00:00Z",
        "period_end": "2026-11-01T00:00:00Z",
        "currency": "USD",
        "cost_kind": "api_usage",
        "evidence_refs": ["ev_usage"],
    }
    cash = ledger.add_period_cost(amount="5", semantic="cashflow", **common)
    consumption = ledger.add_period_cost(amount="4", semantic="consumption", **common)

    first = ledger.export()
    second = ledger.export()
    assert first == second
    assert cash.cost_id != consumption.cost_id
    assert {row["semantic"] for row in first} == {"cashflow", "consumption"}
    assert all("task_id" not in row for row in first)
