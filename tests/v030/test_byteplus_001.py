from __future__ import annotations

import pytest

from collectors.byteplus.collector import (
    DEFAULT_PROFILES,
    DOMAIN_BILLING,
    DOMAIN_CODING_PLAN,
    DOMAIN_INFERENCE,
    BytePlusCollector,
)


def _inputs() -> dict[str, dict[str, object]]:
    return {
        alias: {
            "quota_data": {
                "quota_limit": 1000,
                "quota_used": index * 10,
                "quota_remaining": 1000 - index * 10,
            },
            "inference_data": {
                "model": "seed-code",
                "input_tokens": index * 100,
                "output_tokens": index * 20,
                "requests": index,
            },
            "billing_data": {"cost": str(index), "currency": "USD"},
        }
        for index, alias in enumerate(DEFAULT_PROFILES, start=1)
    }


def test_five_accounts_keep_plan_usage_and_billing_as_isolated_series() -> None:
    collector = BytePlusCollector()
    result = collector.collect_all_accounts(
        trigger="scheduler",
        period_start="2026-10-01T00:00:00Z",
        period_end="2026-11-01T00:00:00Z",
        simulated_accounts_input=_inputs(),
    )
    for index, alias in enumerate(DEFAULT_PROFILES, start=1):
        result.account_runs.append(
            collector.collect_paginated_billing(
                alias,
                pages=[
                    {
                        "items": [
                            {
                                "invoice_id": f"invoice-{alias}",
                                "cost": str(index),
                                "currency": "USD",
                            }
                        ],
                        "has_more": False,
                    }
                ],
                period_start="2026-10-01T00:00:00Z",
                period_end="2026-11-01T00:00:00Z",
            )
        )

    rows = collector.build_isolated_study_rows(result)

    assert {row["account_alias"] for row in rows} == set(DEFAULT_PROFILES)
    for alias in DEFAULT_PROFILES:
        account_rows = [row for row in rows if row["account_alias"] == alias]
        assert {row["product_domain"] for row in account_rows} == {
            DOMAIN_CODING_PLAN,
            DOMAIN_INFERENCE,
            DOMAIN_BILLING,
        }
        assert {row["semantic"] for row in account_rows} == {
            "subscription_plan_allowance",
            "api_inference_usage",
            "provider_billing",
        }
        assert all(row["profile_id"] == alias for row in account_rows)
    assert all("aggregate" not in row for row in rows)


def test_duplicate_account_aliases_and_cross_account_observations_are_refused() -> None:
    with pytest.raises(ValueError, match="must be unique"):
        BytePlusCollector(profiles=["bp01", "bp01"])

    collector = BytePlusCollector()
    result = collector.collect_all_accounts(
        routing_subset=["bp01"],
        simulated_accounts_input={"bp01": _inputs()["bp01"]},
    )
    result.account_runs[0].observations[0].account_alias = "bp02"

    with pytest.raises(ValueError, match="cross-account"):
        collector.build_isolated_study_rows(result)
