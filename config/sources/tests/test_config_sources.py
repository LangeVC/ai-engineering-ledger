"""
Test suite for US-01.a, US-01.b, US-01.c:
- US-01.a: Empty config initialization, offline status & help, re-initialization idempotency (data preservation).
- US-01.b: Nine source types registration, arbitrary account aliases, bp01-bp05 five separate accounts with five distinct health states.
- US-01.c: Listing source, account, collection method, capabilities; distinct verification of unsupported, unverified, unauthorized, ready.
- US-01.d: Unknown major schema version rejection with field name and migration guide.
- Negative test cases & contract invariants.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from config.sources import (
    AELConfig,
    Account,
    Capability,
    ConfigManager,
    ConfigSchemaError,
    HEALTH_DEGRADED,
    HEALTH_READY,
    HEALTH_UNAUTHORIZED,
    HEALTH_UNSUPPORTED,
    HEALTH_UNVERIFIED,
    ProfileBinding,
    STATUS_READY,
    STATUS_UNAUTHORIZED,
    STATUS_UNSUPPORTED,
    STATUS_UNVERIFIED,
    SUPPORTED_SOURCES,
)


class TestConfigSourcesUS01(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config_file = Path(self.temp_dir) / ".ael" / "config.json"
        self.manager = ConfigManager(self.config_file)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_us01_a_initialization_status_and_help_offline(self):
        """
        US-01.a:
        1. Eine leere Konfiguration lässt sich initialisieren.
        2. Status und Hilfe sind ohne Netzwerk erreichbar.
        3. Erneute Initialisierung erhält bestehende Daten.
        """
        # Pre-check: config does not exist
        pre_status = self.manager.status()
        self.assertFalse(pre_status["initialized"])
        self.assertFalse(pre_status["network_required"])

        # 1. Initialize empty config
        cfg, created = self.manager.initialize()
        self.assertTrue(created)
        self.assertTrue(self.config_file.exists())
        self.assertEqual(cfg.schema_version, "0.1")
        self.assertEqual(len(cfg.sources), 0)

        # 2. Check offline status and help
        post_status = self.manager.status()
        self.assertTrue(post_status["initialized"])
        self.assertFalse(post_status["network_required"])
        self.assertEqual(post_status["sources_total"], 0)
        self.assertEqual(post_status["accounts_total"], 0)

        help_text = self.manager.help_offline()
        self.assertIn("Offline Help", help_text)
        self.assertIn("deepseek", help_text)
        self.assertIn("byteplus", help_text)

        # Mutate config by adding a source and account
        cfg.register_source("deepseek")
        cfg.add_account("deepseek", "deepseek-main")
        self.manager.save(cfg)

        status_after_add = self.manager.status()
        self.assertEqual(status_after_add["sources_total"], 1)
        self.assertEqual(status_after_add["accounts_total"], 1)

        # 3. Re-initialize: MUST preserve existing data
        cfg_reinit, created_second = self.manager.initialize(force=False)
        self.assertFalse(created_second, "Re-initialization without force must not overwrite")
        self.assertIn("deepseek", cfg_reinit.sources)
        self.assertIn("deepseek-main", cfg_reinit.sources["deepseek"].accounts)

        # Status still reflects preserved data
        status_after_reinit = self.manager.status()
        self.assertEqual(status_after_reinit["sources_total"], 1)
        self.assertEqual(status_after_reinit["accounts_total"], 1)

    def test_us01_b_nine_source_types_and_bp01_to_bp05_isolation(self):
        """
        US-01.b:
        1. Neun Source-Typen sind registrierbar.
        2. Beliebig viele Account-Aliase sind möglich.
        3. Testfall mit bp01 bis bp05 liefert fünf getrennte Konten und fünf getrennte Health-Zustände.
        """
        cfg, _ = self.manager.initialize()

        # 1. Verify and register all 9 source types
        expected_nine = {
            "deepseek",
            "byteplus",
            "anthropic",
            "openai",
            "google",
            "kilo",
            "faigate",
            "github",
            "forgejo",
        }
        self.assertEqual(SUPPORTED_SOURCES, expected_nine)

        for src_type in expected_nine:
            cfg.register_source(src_type)
        self.assertEqual(len(cfg.sources), 9)

        # 2. Add arbitrary number of aliases (bp01..bp05) to byteplus
        # 3. Assign 5 distinct health states to bp01..bp05
        distinct_health_states = [
            HEALTH_READY,
            HEALTH_DEGRADED,
            HEALTH_UNAUTHORIZED,
            HEALTH_UNVERIFIED,
            HEALTH_UNSUPPORTED,
        ]
        self.assertEqual(len(distinct_health_states), 5)

        for i, health_state in enumerate(distinct_health_states, start=1):
            alias = f"bp0{i}"
            acc = cfg.add_account(
                source_type="byteplus",
                alias=alias,
                profile_id=f"profile-{alias}",
                health=health_state,
            )
            self.assertEqual(acc.health, health_state)
            self.assertEqual(acc.profile_id, f"profile-{alias}")

        self.manager.save(cfg)

        # Re-load from disk to verify serialization roundtrip
        loaded = self.manager.load()
        bp_accounts = loaded.sources["byteplus"].accounts
        self.assertEqual(len(bp_accounts), 5)

        # Verify five separate accounts and five distinct health states
        observed_healths = {}
        for i in range(1, 6):
            alias = f"bp0{i}"
            self.assertIn(alias, bp_accounts)
            acc = bp_accounts[alias]
            self.assertEqual(acc.alias, alias)
            self.assertEqual(acc.profile_id, f"profile-{alias}")
            observed_healths[alias] = acc.health

        # Confirm all 5 accounts have distinct health states
        self.assertEqual(len(set(observed_healths.values())), 5)
        self.assertEqual(set(observed_healths.values()), set(distinct_health_states))

        # Check status aggregation
        status = self.manager.status()
        self.assertEqual(status["sources_total"], 9)
        self.assertEqual(status["accounts_total"], 5)
        for h in distinct_health_states:
            self.assertEqual(status["health_summary"].get(h), 1)

    def test_us01_c_listing_and_capability_status_distinction(self):
        """
        US-01.c:
        Quelle, Account, Erfassungsweg und aktivierte Fähigkeiten werden aufgelistet.
        `unsupported`, `unverified`, `unauthorized` und `ready` sind unterscheidbar.
        """
        cfg, _ = self.manager.initialize()
        cfg.register_source("deepseek", collection_methods=["import_csv", "api_poll"])
        acc_ds = cfg.add_account("deepseek", "deepseek-main")

        # Add 4 capabilities representing the 4 required distinct states
        pb = ProfileBinding(profile_id="deepseek-main", explicit=True, global_state_mutated=False)

        # 1. unsupported
        acc_ds.capabilities.append(
            Capability(
                schema_version="0.1",
                source="deepseek",
                account_alias="deepseek-main",
                product_domain="api",
                capability="traffic",
                status=STATUS_UNSUPPORTED,
                profile_binding=pb,
                checked_at="2026-10-26T12:00:00Z",
                reason="DeepSeek does not provide git traffic endpoints",
            )
        )
        # 2. unverified
        acc_ds.capabilities.append(
            Capability(
                schema_version="0.1",
                source="deepseek",
                account_alias="deepseek-main",
                product_domain="api",
                capability="billing",
                status=STATUS_UNVERIFIED,
                profile_binding=pb,
                checked_at="2026-10-26T12:00:00Z",
                reason="Billing API endpoint not yet probed",
            )
        )
        # 3. unauthorized
        acc_ds.capabilities.append(
            Capability(
                schema_version="0.1",
                source="deepseek",
                account_alias="deepseek-main",
                product_domain="api",
                capability="realtime",
                status=STATUS_UNAUTHORIZED,
                profile_binding=pb,
                checked_at="2026-10-26T12:00:00Z",
                read_only_probe=True,
                evidence_ref="ev_probe_401_unauth",
            )
        )
        # 4. ready
        acc_ds.capabilities.append(
            Capability(
                schema_version="0.1",
                source="deepseek",
                account_alias="deepseek-main",
                product_domain="api",
                capability="usage",
                status=STATUS_READY,
                profile_binding=pb,
                checked_at="2026-10-26T12:00:00Z",
                read_only_probe=True,
                evidence_ref="ev_probe_usage_ok",
            )
        )

        self.manager.save(cfg)

        # Test listing
        entries = cfg.get_source_account_entries()
        self.assertEqual(len(entries), 1)
        entry = entries[0]

        # Verify: Quelle, Account, Erfassungsweg, Fähigkeiten aufgelistet
        self.assertEqual(entry["source"], "deepseek")
        self.assertEqual(entry["account"], "deepseek-main")
        self.assertEqual(entry["collection_methods"], ["import_csv", "api_poll"])
        self.assertEqual(len(entry["capabilities"]), 4)

        statuses = {c["status"] for c in entry["capabilities"]}
        self.assertEqual(
            statuses,
            {STATUS_UNSUPPORTED, STATUS_UNVERIFIED, STATUS_UNAUTHORIZED, STATUS_READY},
        )

    def test_us01_d_negative_schema_major_version_rejected(self):
        """
        US-01.d:
        Konfiguration mit unbekannter Schema-Hauptversion wird vor Datenerfassung
        abgelehnt; Fehlermeldung nennt das Feld und den dokumentierten Migrationsweg.
        """
        invalid_data = {
            "schema_version": "1.0",  # Major version 1 vs supported 0
            "sources": {},
        }

        with self.assertRaises(ConfigSchemaError) as ctx:
            AELConfig.from_dict(invalid_data)

        err = ctx.exception
        self.assertEqual(err.field, "schema_version")
        self.assertIsNotNone(err.migration_guide)
        self.assertIn("docs/migration-v0.1.md", err.migration_guide)
        self.assertIn("Unknown schema major version '1.0'", str(err))

    def test_negative_invalid_source_and_secret_leak_prevention(self):
        """
        Negative probes:
        1. Invalid source type is rejected.
        2. Invalid account alias pattern rejected.
        3. Capability validation failure if required fields missing.
        4. Profile binding non-explicit or global state mutated rejected.
        """
        cfg, _ = self.manager.initialize()

        # 1. Invalid source
        with self.assertRaises(ConfigSchemaError) as ctx:
            cfg.register_source("unsupported_cloud_provider")
        self.assertEqual(ctx.exception.field, "sources.source_type")

        # 2. Invalid alias pattern (e.g. spaces or capital letters)
        with self.assertRaises(ConfigSchemaError) as ctx:
            cfg.add_account("deepseek", "Invalid Alias With Spaces!")
        self.assertEqual(ctx.exception.field, "account.alias")

        # 3. Non-explicit profile binding
        bad_binding = ProfileBinding(profile_id="bp01", explicit=False, global_state_mutated=False)
        with self.assertRaises(ConfigSchemaError) as ctx:
            bad_binding.validate("bp01")
        self.assertEqual(ctx.exception.field, "profile_binding.explicit")

        # 4. Global state mutated profile binding
        bad_binding2 = ProfileBinding(profile_id="bp01", explicit=True, global_state_mutated=True)
        with self.assertRaises(ConfigSchemaError) as ctx:
            bad_binding2.validate("bp01")
        self.assertEqual(ctx.exception.field, "profile_binding.global_state_mutated")

        # 5. Ready capability missing read_only_probe or evidence_ref
        pb = ProfileBinding(profile_id="bp01")
        bad_cap = Capability(
            schema_version="0.1",
            source="byteplus",
            account_alias="bp01",
            product_domain="byteplus_coding_plan",
            capability="quota",
            status=STATUS_READY,
            profile_binding=pb,
            checked_at="2026-10-26T12:00:00Z",
            read_only_probe=False,  # Violation!
            evidence_ref=None,      # Violation!
        )
        with self.assertRaises(ConfigSchemaError) as ctx:
            bad_cap.validate()
        self.assertEqual(ctx.exception.field, "capability.read_only_probe")


if __name__ == "__main__":
    unittest.main()
