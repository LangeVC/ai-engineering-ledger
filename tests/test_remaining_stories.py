from __future__ import annotations

import json
import importlib.util
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from collectors.forge import ForgeSyncState, RepositorySnapshot
from derive.cost import CostEvent, aggregate_costs
from derive.delta import Snapshot, derive_series
from derive.engineering import RepoEquivalence, active_engineering_days, alias_identity, deduplicate_commits
from docs.operations.example_adapter import ExampleAdapter
from export.study import StudyExporter
from reconcile.correlation import Correlation, CorrelationLedger, aggregate_council
from reconcile.coverage import ComparableScope, compare
from storage.recovery import RecoveryManager, redact


class RemainingStoryTests(unittest.TestCase):
    def test_us11_correlation_strength_unassigned_and_council_level(self):
        ledger = CorrelationLedger([
            Correlation("a", "p1", "explicit_source", "ev_1"),
            Correlation("b", "p1", "user_confirmed"),
            Correlation("c", "p1", "heuristic"),
        ])
        result = ledger.partition([{"id": x, "cost": 1} for x in "abcd"], "p1")
        self.assertEqual([x["id"] for x in result["matched"]], ["a", "b"])
        self.assertEqual([x["id"] for x in result["unassigned"]], ["c", "d"])
        self.assertTrue(result["project_filter_excludes_unassigned"])
        parent = {"id": "parent", "council_id": "c1", "tokens": 100}
        kids = [{"id": str(i), "council_id": "c1", "tokens": 10} for i in range(3)]
        self.assertEqual(aggregate_council(parent, kids, "parent")["tokens"], "100")
        self.assertEqual(aggregate_council(parent, kids, "children")["tokens"], "30")

    def test_us12_forge_sync_allowlist_fallback_states_and_reachability(self):
        state = ForgeSyncState({("github", "org/repo")})
        first = RepositorySnapshot("github", "org/repo", "ok",
            commits=({"sha": "a", "committer_at": "2026-01-02T00:00:00Z"},),
            releases=({"id": 1, "name": "v1"},), pull_requests=({"number": 4, "state": "open"},))
        state.sync(first, "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z")
        second = RepositorySnapshot("github", "org/repo", "ok",
            commits=({"sha": "b", "committer_at": "2026-01-03T00:00:00Z"},),
            releases=({"id": 1, "name": "v1 corrected"},), pull_requests=({"number": 4, "state": "closed"},))
        state.sync(second, "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z")
        self.assertFalse(state.commits[("github", "org/repo", "a")]["reachable"])
        self.assertTrue(state.commits[("github", "org/repo", "b")]["reachable"])
        self.assertEqual(state.releases[("github", "org/repo", "1")]["name"], "v1 corrected")
        fallback = RepositorySnapshot("github", "org/repo", "ok", api_available=False,
                                      local_tags=({"name": "v2"},))
        report = state.sync(fallback, "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z")
        self.assertFalse(report["local_tags_classified_as_releases"])
        self.assertEqual(RepositorySnapshot("github", "org/repo", "empty").state, "empty")
        self.assertEqual(RepositorySnapshot("github", "org/repo", "inaccessible").state, "inaccessible")
        with self.assertRaises(PermissionError):
            ForgeSyncState(set()).sync(first, "2026-01-01", "2026-02-01")

    def test_us13_repo_equivalence_activity_and_privacy(self):
        commits = [
            {"repository_id": "forge/r", "sha": "abc", "source": "forgejo", "committer_at": "2026-01-01T23:30:00Z",
             "author_at": "2025-12-31T00:00:00Z", "message": "private"},
            {"repository_id": "github/r", "sha": "abc", "source": "github", "committer_at": "2026-01-01T23:30:00Z"},
            {"repository_id": "other/r", "sha": "abc", "source": "github", "committer_at": "2026-01-02T00:00:00Z"},
        ]
        out = deduplicate_commits(commits, [RepoEquivalence("logical/r", frozenset({"forge/r", "github/r"}))])
        self.assertEqual(len(out["commits"]), 2)
        self.assertNotIn("message", out["source_observations"][0])
        self.assertEqual(active_engineering_days(out["commits"], "Europe/Berlin")["count"], 1)
        self.assertEqual(alias_identity("User@Example.com", b"local"), alias_identity("user@example.com", b"local"))

    def test_us14_delta_reset_rolling_limit_and_late_order(self):
        common = dict(source="s", account="a", product="p", metric="quota", unit="count",
                      window_kind="fixed", definition_version="1")
        series = derive_series([
            Snapshot("m2", "2026-01-02T00:00:00Z", 130, window_id="w1", **common),
            Snapshot("m1", "2026-01-01T00:00:00Z", 100, window_id="w1", **common),
            Snapshot("m3", "2026-02-01T00:00:00Z", 3, window_id="w2", **common),
        ])
        self.assertIsNone(series[0]["delta"])
        self.assertEqual(series[1]["delta"], "30")
        self.assertEqual(series[2]["status"], "reset")
        rolling = {**common, "window_kind": "rolling"}
        net = derive_series([Snapshot("r1", "2026-01-01T00:00:00Z", 10, window_id=None, **rolling),
                             Snapshot("r2", "2026-01-02T00:00:00Z", 7, window_id=None, **rolling)])
        self.assertEqual(net[1]["semantic"], "net_change")
        changed = derive_series([Snapshot("l1", "2026-01-01T00:00:00Z", 50, window_id="w", limit=100, **common),
                                 Snapshot("l2", "2026-01-02T00:00:00Z", 60, window_id="w", limit=200, **common)])
        self.assertEqual(changed[1]["reason"], "budget_or_limit_changed")

    def test_us15_exact_coverage_and_revisions(self):
        scope = ComparableScope("a", "b", "acct", "prod", "requests-v1", "sum", ("route-a",))
        result = compare({"value": 75210, "revision": "p1"}, {"value": 74930, "revision": "r1"}, scope, scope)
        self.assertEqual(result["delta"], "-280")
        self.assertTrue(result["coverage_percent"].startswith("99.6277090812391969"))
        over = compare({"value": 10}, {"value": 11}, scope, scope)
        self.assertEqual(over["status"], "over_coverage_review")
        self.assertFalse(over["accuracy_confirmed"])
        self.assertEqual(compare({"value": 0}, {"value": 1}, scope, scope)["status"], "deviation_undefined")
        self.assertEqual(compare(None, {"value": 1}, None, scope)["status"], "provider_missing")

    def test_us16_cost_categories_currency_entitlement_and_decimal(self):
        events = [
            CostEvent("s1", "subscription_fee", "20.00", "USD", "chatgpt-plus"),
            CostEvent("s2", "subscription_fee", "20.00", "USD", "chatgpt-plus"),
            CostEvent("u1", "api_usage_measured", "0.10", "USD"),
            CostEvent("u2", "api_usage_measured", "0.20", "USD"),
            CostEvent("p1", "credit_purchase", "10", "EUR"),
            CostEvent("c1", "credit_consumption", "4", "EUR"),
            CostEvent("x1", "unknown_fee", "2", "USD"),
            CostEvent("fx1", "api_usage_estimated", "10", "EUR", fx_rate="1.1",
                      fx_date="2026-01-01", fx_source="synthetic-fixture", fx_target_currency="USD"),
        ]
        report = aggregate_costs(events)
        self.assertEqual(report["categories"]["subscription_fee"]["USD"], "20.00")
        self.assertEqual(report["categories"]["api_usage_measured"]["USD"], "0.30")
        self.assertEqual(report["cashflow_view"]["EUR"], "10")
        self.assertEqual(report["consumption_view"]["EUR"], "14")
        self.assertIn("USD", report["unknown_not_free"])
        self.assertEqual(report["fx_conversions"][0]["converted_amount"], "11.0")
        with self.assertRaises(ValueError):
            CostEvent("fx", "api_usage_measured", "1", "USD", fx_rate="0.9")

    def test_us17_export_is_deterministic_safe_and_schema_complete(self):
        tables = {"dataset": [{"measurement_id": "=formula", "observed_at_utc": "2026-01-01T00:00:00Z",
                               "observed_at_source_offset": "+00:00", "source_alias": "s", "account_alias": "a",
                               "provider": "p", "model": "=bad", "input_tokens": 1, "output_tokens": 2,
                               "cost": "0.30", "currency": "USD", "confidence": "high"}]}
        with tempfile.TemporaryDirectory() as td:
            a, b = Path(td) / "a", Path(td) / "b"
            kwargs = dict(filters={"from": "2026-01-01", "to": "2026-02-01"}, timezone_name="UTC",
                          study_cutoff="2026-02-01T00:00:00Z",
                          data_state={"id": "ds-12345678", "as_of": "2026-02-01T00:00:00Z"})
            m1 = StudyExporter().write(a, tables, export_id="one", export_timestamp="2026-02-01T00:00:00Z", **kwargs)
            m2 = StudyExporter().write(b, tables, export_id="two", export_timestamp="2026-02-02T00:00:00Z", **kwargs)
            self.assertEqual(m1["content_hash"], m2["content_hash"])
            self.assertEqual((a / "dataset.csv").read_bytes(), (b / "dataset.csv").read_bytes())
            self.assertTrue((a / "dataset.csv").read_text().splitlines()[1].startswith("'=formula"))
            self.assertEqual(len(m1["tables"]), 8)
            self.assertEqual(json.loads((a / "dataset.json").read_text())["dataset"][0]["model"], "=bad")
            validator_path = Path(__file__).resolve().parents[1] / "schema/export/tests/validate.py"
            spec = importlib.util.spec_from_file_location("ael_export_validator", validator_path)
            validator = importlib.util.module_from_spec(spec); spec.loader.exec_module(validator)
            schema = json.loads((Path(__file__).resolve().parents[1] / "schema/export/manifest.schema.json").read_text())
            self.assertEqual(validator.validate(m1, schema), [])
            with self.assertRaises(ValueError):
                StudyExporter().write(Path(td) / "unsafe", {"dataset": [{"model": "Bearer secret-token"}]}, **kwargs)

    def test_us18_permissions_redaction_backup_restore_migration_deletion(self):
        manager = RecoveryManager()
        self.assertNotIn("sk_TESTSECRET", json.dumps(redact({"error": "sk_TESTSECRET", "path": "/Users/name/file"})))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); db = root / "db"
            db.write_bytes(b"fixture-db"); evidence = root / "ev"; evidence.write_bytes(b"fixture-evidence")
            backup, restored = root / "backup", root / "restored"
            manifest = manager.backup(backup, db, {"token": "secret", "name": "safe"}, [evidence])
            manager.restore(backup, restored)
            self.assertEqual((restored / "ledger.db").read_bytes(), b"fixture-db")
            self.assertEqual(os.stat(restored).st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(restored / "ledger.db").st_mode & 0o777, 0o600)
            original = db.read_bytes()
            def fail(path):
                path.write_bytes(b"half"); raise RuntimeError("boom")
            with self.assertRaises(RuntimeError):
                manager.migrate_transactionally(db, fail)
            self.assertEqual(db.read_bytes(), original)
            records = [{"id": "1", "account": "a", "evidence_ids": ["ev1"]}, {"id": "2", "account": "b"}]
            self.assertTrue(manager.deletion_preview(records, "a")["exports_no_longer_reproducible"])
            remaining, tombstone = manager.delete(records, "a", True)
            self.assertEqual([r["id"] for r in remaining], ["2"])
            self.assertNotIn("account", tombstone)
            self.assertNotIn("evidence", tombstone)

    def test_us19_example_adapter_contract(self):
        adapter = ExampleAdapter()
        self.assertEqual(adapter.capabilities()[0]["status"], "ready")
        rows = adapter.normalize({"alias": "demo", "secret_ref": "env:EXAMPLE"}, adapter.collect(
            {"alias": "demo", "secret_ref": "env:EXAMPLE"}, [{"id": "1", "metric": "tokens", "value": 2, "unit": "tokens"}]))
        self.assertEqual(len(rows[0]["idempotency_key"]), 64)
        with self.assertRaises(ValueError):
            adapter.validate({"alias": "demo", "token": "do-not-store"})


if __name__ == "__main__":
    unittest.main()
