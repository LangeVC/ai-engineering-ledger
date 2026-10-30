from __future__ import annotations

import pytest

from collectors.kilo.adapter import KiloAdapter, KiloValidationError


SESSION = {
    "version": "1.0",
    "session": {
        "id": "ses_explicit_123",
        "project_id": "proj_explicit_123",
        "tokens_input": 10,
        "tokens_output": 2,
        "time_created": 1790000000000,
        "time_updated": 1790000001000,
    },
    "messages": [
        {
            "id": "req_explicit_123",
            "type": "assistant",
            "tokens": {"input": 10, "output": 2},
            "time_created": 1790000000000,
        }
    ],
}

STATS = {"version": "1.0", "stats": {"project_id": "proj_explicit_123", "days": 7, "tokens_input": 10}}


def test_dry_run_is_read_only_idempotent_and_scope_explicit() -> None:
    adapter = KiloAdapter("kilocode-main")
    first = adapter.dry_run_session(SESSION)
    second = adapter.dry_run_session(SESSION)
    aggregate = adapter.dry_run_stats(STATS)

    assert first["writes"] == 0
    assert first["idempotency_keys"] == second["idempotency_keys"]
    assert first["coverage"]["scope_levels"] == ["session", "request"]
    assert aggregate["coverage"]["scope_levels"] == ["aggregate"]
    assert first["capability"]["billing_truth"] is False
    assert first["capability"]["scope_policy"]["mixed_token_sum"] == "forbidden"


def test_aggregate_and_request_scopes_cannot_be_summed() -> None:
    adapter = KiloAdapter()
    _session, requests = adapter.parse_session(SESSION)
    aggregate = adapter.parse_stats(STATS)
    with pytest.raises(KiloValidationError):
        adapter.assert_aggregation_safety([aggregate, *requests])
