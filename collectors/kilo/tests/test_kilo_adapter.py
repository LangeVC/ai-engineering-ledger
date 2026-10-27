"""Contract and unit tests for Kilo Adapter (collectors/kilo).

Verifies binary criteria and PRD US-10:
- US-10.a: Minimierte reale und synthetische Kilo-Session-JSONs fehlerfrei importieren
- US-10.b: Kilo-Aggregate und Session-Requests dürfen nicht unkontrolliert doppelt summiert werden
- US-10.c: Provider, Modell, Tokens, Kosten, Latenzen normalisiert; Kosten als execution_observation
- US-10.d: IDs (session_id, project_id, workspace_id, parent_id, council_id, task_id) erhalten,
           keine fremden IDs erfinden (bleiben None/null)
- Validierung fehlerhafter Payloads (negative Tokens, ungültiges JSON, fehlende session.id)
- Idempotenz: Wiederholtes Parsen erzeugt identische Idempotency-Keys
"""

import json
import os
import unittest

from collectors.kilo.adapter import (
    KiloAdapter,
    KiloAggregateRecord,
    KiloParseError,
    KiloRequestRecord,
    KiloSessionRecord,
    KiloValidationError,
    parse_kilo_export,
    parse_kilo_stats,
)

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


class TestKiloAdapter(unittest.TestCase):

    def setUp(self) -> None:
        self.adapter = KiloAdapter(account_alias="local-kilo")

    def test_us10_a_real_fixture_import(self) -> None:
        """US-10.a: Import real minimized Kilo session fixture without inventing fields."""
        real_path = os.path.join(FIXTURES_DIR, "valid_session_real.json")
        with open(real_path, "r", encoding="utf-8") as f:
            content = f.read()

        session_rec, requests = self.adapter.parse_session(content)

        # Version documented
        self.assertEqual(session_rec.raw_source_version, "7.0.46")

        # Invariant US-10.d: IDs preserved, missing IDs remain None
        self.assertEqual(session_rec.session_id, "ses_ea1986d1cffekCBgIdOHaxu20e")
        self.assertEqual(session_rec.project_id, "39e238b24c5dbae1c5f351b8c38ad0a689b50ded")
        self.assertIsNone(session_rec.workspace_id)
        self.assertIsNone(session_rec.parent_id)
        self.assertIsNone(session_rec.council_id)
        self.assertIsNone(session_rec.task_id)

        # Metrics normalized
        self.assertEqual(session_rec.tokens_input, 131645)
        self.assertEqual(session_rec.tokens_output, 36040)
        self.assertEqual(session_rec.tokens_reasoning, 27853)
        self.assertEqual(session_rec.tokens_cache_read, 4463744)
        self.assertEqual(session_rec.tokens_cache_write, 0)

        # Invariant US-10.c: Cost semantic is execution observation, NOT subscription billing
        self.assertEqual(session_rec.cost_usd, 0.0)
        self.assertEqual(session_rec.cost_semantic, "execution_observation")
        self.assertEqual(session_rec.model_id, "deepseek-v4-pro")
        self.assertEqual(session_rec.provider_id, "faigate")

        # Requests parsed
        self.assertGreaterEqual(len(requests), 1)
        req9 = next((r for r in requests if r.request_id == "msg_15e6794150010PJHIZKt31ZQ5L"), None)
        self.assertIsNotNone(req9)
        self.assertEqual(req9.tokens_input, 16046)
        self.assertEqual(req9.tokens_output, 96)
        self.assertEqual(req9.tokens_reasoning, 71)
        self.assertEqual(req9.cost_semantic, "execution_observation")
        self.assertEqual(req9.model_id, "deepseek-v4-pro")
        self.assertEqual(req9.provider_id, "faigate")

        # Latency calculated from time_created and time_updated (1792585214656 - 1792585208866 = 5790ms)
        self.assertEqual(req9.latency_ms, 5790.0)

    def test_us10_d_synthetic_council_correlation(self) -> None:
        """US-10.d: Retain explicit council_id and task_id when present in source payload."""
        synth_path = os.path.join(FIXTURES_DIR, "valid_session_synthetic.json")
        with open(synth_path, "r", encoding="utf-8") as f:
            content = f.read()

        session_rec, requests = self.adapter.parse_session(content)

        self.assertEqual(session_rec.session_id, "ses_synth_council_12345")
        self.assertEqual(session_rec.project_id, "proj_synth_999")
        self.assertEqual(session_rec.workspace_id, "ws_alpha")
        self.assertEqual(session_rec.parent_id, "ses_parent_000")
        self.assertEqual(session_rec.council_id, "cncl_run_42")
        self.assertEqual(session_rec.task_id, "task_issue_88")
        self.assertEqual(session_rec.cost_usd, 0.05)

        self.assertEqual(len(requests), 1)
        req = requests[0]
        self.assertEqual(req.council_id, "cncl_run_42")
        self.assertEqual(req.task_id, "task_issue_88")
        self.assertEqual(req.latency_ms, 2000.0)
        self.assertEqual(req.cost_semantic, "execution_observation")

    def test_us10_d_external_council_correlation(self) -> None:
        """US-10.d: Explicit external council correlation binds to session and requests."""
        real_path = os.path.join(FIXTURES_DIR, "valid_session_real.json")
        with open(real_path, "r", encoding="utf-8") as f:
            content = f.read()

        session_rec, requests = self.adapter.parse_session(
            content,
            external_council_id="cncl_external_100",
            external_task_id="task_jira_200",
        )

        self.assertEqual(session_rec.council_id, "cncl_external_100")
        self.assertEqual(session_rec.task_id, "task_jira_200")
        for r in requests:
            self.assertEqual(r.council_id, "cncl_external_100")
            self.assertEqual(r.task_id, "task_jira_200")

    def test_us10_b_aggregate_stats_parsing(self) -> None:
        """US-10.b: Kilo aggregate stats parsed with separate aggregate scope level."""
        stats_path = os.path.join(FIXTURES_DIR, "valid_aggregate_stats.json")
        with open(stats_path, "r", encoding="utf-8") as f:
            content = f.read()

        agg = self.adapter.parse_stats(content)

        self.assertEqual(agg.scope_level, "aggregate")
        self.assertEqual(agg.project_id, "39e238b24c5dbae1c5f351b8c38ad0a689b50ded")
        self.assertEqual(agg.days, 7)
        self.assertEqual(agg.tokens_input, 131645)
        self.assertEqual(agg.tokens_output, 36040)
        self.assertEqual(agg.tokens_cache_read, 4463744)
        self.assertEqual(agg.total_cost_usd, 0.0)
        self.assertEqual(agg.cost_semantic, "execution_observation")

    def test_us10_b_invariant_double_summing_protection(self) -> None:
        """US-10.b: Kilo aggregate and session requests must not be summed together."""
        real_path = os.path.join(FIXTURES_DIR, "valid_session_real.json")
        with open(real_path, "r", encoding="utf-8") as f:
            session_rec, requests = self.adapter.parse_session(f.read())

        stats_path = os.path.join(FIXTURES_DIR, "valid_aggregate_stats.json")
        with open(stats_path, "r", encoding="utf-8") as f:
            agg = self.adapter.parse_stats(f.read())

        # Attempting to combine aggregate and session/request in unsegregated pool raises error
        with self.assertRaises(KiloValidationError):
            self.adapter.assert_aggregation_safety([session_rec, agg])

        with self.assertRaises(KiloValidationError):
            self.adapter.assert_aggregation_safety([*requests, agg])

        # Separate collections pass safely
        self.adapter.assert_aggregation_safety([session_rec, *requests])
        self.adapter.assert_aggregation_safety([agg])

    def test_idempotency_keys_are_deterministic(self) -> None:
        """Idempotency keys must be stable and deterministic across multiple runs."""
        real_path = os.path.join(FIXTURES_DIR, "valid_session_real.json")
        with open(real_path, "r", encoding="utf-8") as f:
            content = f.read()

        s1, reqs1 = self.adapter.parse_session(content)
        s2, reqs2 = self.adapter.parse_session(content)

        self.assertEqual(s1.idempotency_key, s2.idempotency_key)
        self.assertEqual(len(reqs1), len(reqs2))
        for r1, r2 in zip(reqs1, reqs2):
            self.assertEqual(r1.idempotency_key, r2.idempotency_key)

    def test_invalid_missing_session_id(self) -> None:
        """Missing session id must fail validation."""
        inv_path = os.path.join(FIXTURES_DIR, "invalid_missing_session_id.json")
        with open(inv_path, "r", encoding="utf-8") as f:
            content = f.read()

        with self.assertRaises(KiloValidationError):
            self.adapter.parse_session(content)

    def test_invalid_negative_tokens(self) -> None:
        """Negative tokens must fail validation."""
        inv_path = os.path.join(FIXTURES_DIR, "invalid_negative_tokens.json")
        with open(inv_path, "r", encoding="utf-8") as f:
            content = f.read()

        with self.assertRaises(KiloValidationError):
            self.adapter.parse_session(content)

    def test_invalid_corrupted_json(self) -> None:
        """Corrupted JSON must raise KiloParseError."""
        with self.assertRaises(KiloParseError):
            self.adapter.parse_session("{corrupted: json")


if __name__ == "__main__":
    unittest.main()
