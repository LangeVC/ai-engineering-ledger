"""Unit and integration tests for Runtime Collector & Aggregator (collectors/runtime).

Strictly verifies PRD US-10:
- US-10.a: Import real minimized Kilo session fixture and real faigate export fixture
           with documented version. Missing fields are never guessed, only explicit derived values.
- US-10.b: Kilo-Stats as aggregates and session requests covering the same scope
           must NOT be summed together; provenance and scope levels remain distinct.
           Negative probe explicitly verifies RuntimeAggregationError is raised.
- US-10.c: Provider, model, tokens, costs, and latencies normalized only when present in source.
           billing_assertion is strictly False; cost is marked execution_observation, never subscription billing.
- US-10.d: Identifier retention (session_id, project_id, task_id, council_id).
           Missing IDs remain None; working directory does not forge association.
"""

import json
import os
import unittest

from collectors.runtime.collector import (
    RuntimeCollector,
    RuntimeObservation,
    RuntimeScopeError,
    RuntimeAggregationError,
    RuntimeValidationError,
    aggregate_runtime_observations,
    reconcile_runtime_with_provider,
)

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


class TestRuntimeCollectorUS10(unittest.TestCase):

    def setUp(self) -> None:
        self.collector = RuntimeCollector(account_alias="test_runtime")

    def test_us10_a_kilo_real_session_import(self) -> None:
        """US-10.a: Import real minimized Kilo session fixture with documented version 7.0.46."""
        fixture_path = os.path.join(FIXTURES_DIR, "kilo-session-fixture.json")
        with open(fixture_path, "r", encoding="utf-8") as f:
            content = f.read()

        session_obs, requests = self.collector.import_kilo_session(content)

        # Documented source and version
        self.assertEqual(session_obs.source, "kilo")
        self.assertEqual(session_obs.source_version, "7.0.46")
        self.assertEqual(session_obs.scope_level, "session")

        # Invariant US-10.c: execution observation, no billing assertion
        self.assertFalse(session_obs.billing_assertion)
        self.assertEqual(session_obs.cost_semantic, "execution_observation")
        self.assertEqual(session_obs.cost_usd, 0.0)

        # Identifiers preserved (US-10.d)
        self.assertEqual(session_obs.session_id, "ses_ea1986d1cffekCBgIdOHaxu20e")
        self.assertEqual(session_obs.project_id, "39e238b24c5dbae1c5f351b8c38ad0a689b50ded")
        self.assertIsNone(session_obs.task_id)
        self.assertIsNone(session_obs.council_id)
        self.assertIsNone(session_obs.repository_id)

        # Metrics normalized
        self.assertEqual(session_obs.tokens_input, 131645)
        self.assertEqual(session_obs.tokens_output, 36040)
        self.assertEqual(session_obs.tokens_reasoning, 27853)
        self.assertEqual(session_obs.tokens_cache_read, 4463744)
        self.assertEqual(session_obs.tokens_cache_write, 0)

        # Explicit derived metric with formula total_tokens = input + output
        self.assertEqual(session_obs.total_tokens, 131645 + 36040)
        self.assertEqual(session_obs.provider, "faigate")
        self.assertEqual(session_obs.model, "deepseek-v4-pro")
        self.assertEqual(session_obs.agent, "build")

        # Requests in session
        self.assertEqual(len(requests), 3)
        req_msg9 = next((r for r in requests if r.request_id == "msg_15e6794150010PJHIZKt31ZQ5L"), None)
        self.assertIsNotNone(req_msg9)
        self.assertEqual(req_msg9.scope_level, "request")
        self.assertEqual(req_msg9.tokens_input, 16046)
        self.assertEqual(req_msg9.tokens_output, 96)
        self.assertEqual(req_msg9.tokens_reasoning, 71)
        self.assertEqual(req_msg9.total_tokens, 16046 + 96)
        self.assertEqual(req_msg9.latency_ms, 5790.0)
        self.assertFalse(req_msg9.billing_assertion)

    def test_us10_a_faigate_real_export_import(self) -> None:
        """US-10.a: Import real faigate export fixture with documented version 2.10.2."""
        fixture_path = os.path.join(FIXTURES_DIR, "faigate-export-fixture.json")
        with open(fixture_path, "r", encoding="utf-8") as f:
            content = f.read()

        observations = self.collector.import_faigate_export(content)

        self.assertEqual(len(observations), 1)
        obs = observations[0]

        # Documented source and version
        self.assertEqual(obs.source, "faigate")
        self.assertEqual(obs.source_version, "2.10.2")
        self.assertEqual(obs.scope_level, "request")
        self.assertEqual(obs.request_id, "1")

        # Invariant US-10.c: billing_assertion is strictly False
        self.assertFalse(obs.billing_assertion)
        self.assertEqual(obs.cost_semantic, "execution_observation")
        self.assertEqual(obs.cost_usd, 0.0032)

        # Metrics normalized
        self.assertEqual(obs.tokens_input, 16046)
        self.assertEqual(obs.tokens_output, 96)
        self.assertEqual(obs.tokens_cache_read, 0)
        self.assertEqual(obs.total_tokens, 16046 + 96)
        self.assertEqual(obs.latency_ms, 320.5)

        # Routing information
        self.assertEqual(obs.provider, "deepseek")
        self.assertEqual(obs.model, "deepseek-chat")
        self.assertTrue(obs.success)
        self.assertIsNone(obs.error)

    def test_us10_b_aggregate_and_requests_cannot_be_summed_together(self) -> None:
        """Negative probe for US-10.b: Kilo aggregate stats and session requests covering the same scope
        MUST NOT be summed together into an unsegregated total.
        """
        # Load real session fixture
        kilo_path = os.path.join(FIXTURES_DIR, "kilo-session-fixture.json")
        with open(kilo_path, "r", encoding="utf-8") as f:
            session_obs, requests = self.collector.import_kilo_session(f.read())

        # Load aggregate stats fixture
        stats_path = os.path.join(FIXTURES_DIR, "valid_aggregate_stats.json")
        with open(stats_path, "r", encoding="utf-8") as f:
            agg_obs = self.collector.import_kilo_stats(f.read())

        # Negative Probe 1: Attempting to aggregate aggregate-level and session-level metrics raises error
        with self.assertRaises(RuntimeAggregationError) as ctx1:
            aggregate_runtime_observations([agg_obs, session_obs])
        self.assertIn("Invariant US-10.b violation", str(ctx1.exception))
        self.assertIn("cannot sum aggregate-level and request/session-level", str(ctx1.exception))

        # Negative Probe 2: Attempting to aggregate aggregate-level and request-level metrics raises error
        with self.assertRaises(RuntimeAggregationError) as ctx2:
            aggregate_runtime_observations([agg_obs, *requests])
        self.assertIn("Invariant US-10.b violation", str(ctx2.exception))

        # Negative Probe 3: Attempting to sum session parent and its own request children raises error
        with self.assertRaises(RuntimeAggregationError) as ctx3:
            aggregate_runtime_observations([session_obs, *requests])
        self.assertIn("both session-level aggregate and individual request-level", str(ctx3.exception))

        # Positive Case 1: Pure aggregate collection aggregates safely
        agg_summary = aggregate_runtime_observations([agg_obs])
        self.assertEqual(agg_summary["total_observations"], 1)
        self.assertEqual(agg_summary["total_tokens_input"], 131645)
        self.assertFalse(agg_summary["billing_assertion"])
        self.assertIn("kilo:aggregate", agg_summary["scope_breakdown"])

        # Positive Case 2: Pure granular requests aggregate safely
        req_summary = aggregate_runtime_observations(requests)
        self.assertEqual(req_summary["total_observations"], 3)
        self.assertEqual(req_summary["total_tokens_input"], 16046)
        self.assertFalse(req_summary["billing_assertion"])
        self.assertIn("kilo:request", req_summary["scope_breakdown"])

    def test_us10_c_billing_assertion_invariants(self) -> None:
        """US-10.c: Gateway observation is pure Execution Truth, never subscription billing truth."""
        faigate_path = os.path.join(FIXTURES_DIR, "faigate-export-fixture.json")
        with open(faigate_path, "r", encoding="utf-8") as f:
            observations = self.collector.import_faigate_export(f.read())

        summary = aggregate_runtime_observations(observations)
        self.assertFalse(summary["billing_assertion"])
        self.assertEqual(summary["cost_semantic"], "execution_observation")
        self.assertEqual(summary["total_cost_usd"], 0.0032)

        # Negative probe: RuntimeObservation cannot be constructed with billing_assertion=True
        with self.assertRaises(RuntimeValidationError) as ctx:
            RuntimeObservation(
                observation_id="illegal_billing",
                source="faigate",
                source_version="2.10.2",
                scope_level="request",
                session_id=None,
                project_id=None,
                repository_id=None,
                task_id=None,
                council_id=None,
                request_id="1",
                tokens_input=10,
                tokens_output=10,
                tokens_reasoning=None,
                tokens_cache_read=None,
                tokens_cache_write=None,
                total_tokens=20,
                latency_ms=100.0,
                cost_usd=0.01,
                billing_assertion=True,  # ILLEGAL
                cost_semantic="execution_observation",
                provider="deepseek",
                model="deepseek-chat",
                agent=None,
                success=True,
                error=None,
                created_at_iso=None,
                observed_at_iso=None,
                idempotency_key="abc",
            )
        self.assertIn("Invariant US-10.c violation", str(ctx.exception))

    def test_reconciliation_formula(self) -> None:
        """US-10.c / US-15 reconciliation: coverage = observed / authoritative."""
        # 16046 tokens observed, provider says 16046
        rec_exact = reconcile_runtime_with_provider(16046, 16046)
        self.assertEqual(rec_exact["status"], "exact_match")
        self.assertEqual(rec_exact["coverage_ratio"], 1.0)
        self.assertEqual(rec_exact["delta"], 0)

        # 16000 observed, provider says 16046 (slight under-coverage)
        rec_under = reconcile_runtime_with_provider(16000, 16046)
        self.assertEqual(rec_under["status"], "under_coverage")
        self.assertTrue(rec_under["coverage_ratio"] < 1.0)
        self.assertEqual(rec_under["delta"], -46)

        # Zero usage
        rec_zero = reconcile_runtime_with_provider(0, 0)
        self.assertEqual(rec_zero["status"], "no_usage")
        self.assertIsNone(rec_zero["coverage_ratio"])

    def test_negative_corrupted_json(self) -> None:
        """Negative probe: Corrupted JSON must fail with RuntimeValidationError."""
        with self.assertRaises(RuntimeValidationError):
            self.collector.import_kilo_session("{malformed json:")

        with self.assertRaises(RuntimeValidationError):
            self.collector.import_faigate_export("not valid json")


if __name__ == "__main__":
    unittest.main()
