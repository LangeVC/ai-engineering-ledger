from __future__ import annotations

import pytest

from importers.manual.importer import ManualImportValidationError, build_subscription_plan_snapshot


@pytest.mark.parametrize("template", ["claude_pro", "codex_plus", "antigravity"])
def test_subscription_snapshot_has_human_provenance_and_explicit_unknowns(template: str) -> None:
    first = build_subscription_plan_snapshot(
        template,
        f"{template}-account",
        "2026-10-29T12:00:00+01:00",
        plan_name=None,
        amount=None,
        billing_period=None,
        evidence_id="ev_manual_123",
    )
    second = build_subscription_plan_snapshot(
        template,
        f"{template}-account",
        "2026-10-29T12:00:00+01:00",
        evidence_id="ev_manual_123",
    )

    assert first["idempotency_key"] == second["idempotency_key"]
    assert first["plan_name"]["state"] == "unknown"
    assert first["amount"]["state"] == "unknown"
    assert first["billing_period"]["state"] == "unknown"
    assert first["usage"]["state"] == "unknown"
    assert first["provenance"] == {
        "class": "P1",
        "method": "human_manual_entry",
        "human_confirmed": True,
        "evidence_id": "ev_manual_123",
    }


def test_known_subscription_cost_requires_currency() -> None:
    with pytest.raises(ManualImportValidationError):
        build_subscription_plan_snapshot(
            "claude_pro",
            "claude-pro",
            "2026-10-29T12:00:00+01:00",
            amount="20.00",
        )


def test_manual_snapshot_requires_human_confirmation() -> None:
    with pytest.raises(ManualImportValidationError):
        build_subscription_plan_snapshot(
            "antigravity",
            "antigravity-pro",
            "2026-10-29T12:00:00+01:00",
            human_confirmed=False,
        )
