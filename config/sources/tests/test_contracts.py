"""
Contract tests validating config/sources against schema/collector JSON schemas.
Validates account and capability generation against JSON Schema draft 2020-12 if jsonschema is available,
and performs structural invariant contract checking.
"""

import json
import unittest
from pathlib import Path

from config.sources import (
    AELConfig,
    Capability,
    HEALTH_READY,
    HEALTH_UNAUTHORIZED,
    HEALTH_UNSUPPORTED,
    HEALTH_UNVERIFIED,
    ProfileBinding,
    STATUS_READY,
    STATUS_UNAUTHORIZED,
    STATUS_UNSUPPORTED,
    STATUS_UNVERIFIED,
)

try:
    import jsonschema
    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False


class TestConfigSourcesContract(unittest.TestCase):
    def setUp(self):
        # Locate project root schemas
        self.worktree_root = Path(__file__).resolve().parents[3]
        self.main_repo_root = self.worktree_root.parents[2]
        self.collector_schema_dir = self.main_repo_root / "schema" / "collector"

    def test_account_contract_invariants(self):
        """Verify generated Account dict strictly conforms to account.schema.json structure."""
        cfg = AELConfig()
        cfg.register_source("byteplus")
        acc = cfg.add_account(
            source_type="byteplus",
            alias="bp01",
            profile_id="bp01",
            health=HEALTH_READY,
        )

        pb = ProfileBinding(profile_id="bp01", explicit=True, global_state_mutated=False)
        cap = Capability(
            schema_version="0.1",
            source="byteplus",
            account_alias="bp01",
            product_domain="byteplus_coding_plan",
            capability="quota",
            status=STATUS_READY,
            profile_binding=pb,
            checked_at="2026-10-26T12:00:00Z",
            read_only_probe=True,
            evidence_ref="ev_bp01_quota_ok",
        )
        acc.capabilities.append(cap)

        acc_dict = acc.to_dict()
        required_keys = {"schema_version", "source", "alias", "profile_id", "product_domains", "capabilities", "health"}
        self.assertTrue(required_keys.issubset(set(acc_dict.keys())))
        self.assertEqual(acc_dict["schema_version"], "0.1")
        self.assertEqual(acc_dict["source"], "byteplus")
        self.assertEqual(acc_dict["alias"], "bp01")
        self.assertEqual(acc_dict["health"], "ready")

        cap_dict = acc_dict["capabilities"][0]
        required_cap_keys = {
            "schema_version",
            "source",
            "account_alias",
            "product_domain",
            "capability",
            "status",
            "profile_binding",
            "checked_at",
        }
        self.assertTrue(required_cap_keys.issubset(set(cap_dict.keys())))
        self.assertTrue(cap_dict["profile_binding"]["explicit"])
        self.assertFalse(cap_dict["profile_binding"]["global_state_mutated"])

    def test_jsonschema_validation_if_available(self):
        """If jsonschema is installed and schemas exist, validate accounts against account.schema.json."""
        if not HAS_JSONSCHEMA:
            self.skipTest("jsonschema not installed in runtime")

        account_schema_file = self.collector_schema_dir / "account.schema.json"
        if not account_schema_file.exists():
            self.skipTest(f"Schema not found at {account_schema_file}")

        common_schema_file = self.collector_schema_dir / "common.schema.json"
        cap_schema_file = self.collector_schema_dir / "capability.schema.json"

        acc_schema = json.loads(account_schema_file.read_text(encoding="utf-8"))
        common_schema = json.loads(common_schema_file.read_text(encoding="utf-8"))
        cap_schema = json.loads(cap_schema_file.read_text(encoding="utf-8"))

        # Build schema registry / resolver
        schema_store = {
            acc_schema.get("$id", "account.schema.json"): acc_schema,
            "https://ael.local/schema/collector/common.schema.json": common_schema,
            "common.schema.json": common_schema,
            "https://ael.local/schema/collector/capability.schema.json": cap_schema,
            "capability.schema.json": cap_schema,
        }

        # Create sample test account
        cfg = AELConfig()
        cfg.register_source("byteplus")
        acc = cfg.add_account("byteplus", "bp01", profile_id="bp01", health=HEALTH_READY)
        pb = ProfileBinding(profile_id="bp01", explicit=True, global_state_mutated=False)
        cap = Capability(
            schema_version="0.1",
            source="byteplus",
            account_alias="bp01",
            product_domain="byteplus_coding_plan",
            capability="quota",
            status=STATUS_READY,
            profile_binding=pb,
            checked_at="2026-10-26T12:00:00Z",
            read_only_probe=True,
            evidence_ref="ev_bp01_quota_ok",
        )
        acc.capabilities.append(cap)

        data = acc.to_dict()

        # Validate with jsonschema Draft202012Validator
        registry = jsonschema.validators.Draft202012Validator
        # In modern jsonschema, we can check or validate
        resolver = jsonschema.RefResolver.from_schema(acc_schema, store=schema_store)
        validator = registry(acc_schema, resolver=resolver)
        validator.validate(data)


if __name__ == "__main__":
    unittest.main()
