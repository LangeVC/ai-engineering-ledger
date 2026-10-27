"""
Contract validation test for storage/evidence descriptors.
Validates generated descriptors against schema/core/evidence.schema.json.
"""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from storage.evidence import (
    EvidenceStore,
    RedactionInfo,
    generate_evidence_id,
    compute_sha256,
)

try:
    import jsonschema
    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False


class TestEvidenceContract(unittest.TestCase):
    def setUp(self):
        # Locate core schema files in repository
        repo_root = Path(__file__).resolve().parents[3]
        self.schema_dir = repo_root / "schema" / "core"
        if not self.schema_dir.exists():
            # Check sibling worktree or root
            alt = repo_root.parent / "ael-contract-event" / "schema" / "core"
            if alt.exists():
                self.schema_dir = alt

    def test_evidence_descriptor_conforms_to_schema(self):
        if not HAS_JSONSCHEMA:
            self.skipTest("jsonschema not available in environment")

        ev_schema_path = self.schema_dir / "evidence.schema.json"
        common_schema_path = self.schema_dir / "common.schema.json"
        if not ev_schema_path.exists():
            self.skipTest(f"evidence.schema.json not found at {ev_schema_path}")

        ev_schema = json.loads(ev_schema_path.read_text())
        common_schema = json.loads(common_schema_path.read_text())

        schema_store = {
            ev_schema.get("$id", "https://ael.invalid/schema/core/0.1/evidence.schema.json"): ev_schema,
            common_schema.get("$id", "https://ael.invalid/schema/core/0.1/common.schema.json"): common_schema,
            "common.schema.json": common_schema,
        }

        resolver = jsonschema.RefResolver.from_schema(ev_schema, store=schema_store)
        validator = jsonschema.Draft202012Validator(ev_schema, resolver=resolver)

        # 1. Unredacted descriptor
        desc1 = {
            "schema_version": "0.1",
            "evidence_id": generate_evidence_id(),
            "sha256": compute_sha256(b"hello world"),
            "size_bytes": 11,
            "mime_type": "text/plain",
            "source": "deepseek",
            "account": "primary",
            "created_at": "2026-10-26T12:00:00Z",
            "collector_version": "1.0.0",
            "capture_point": "provider_response_verbatim",
            "redaction": {
                "redacted": False,
                "redaction_version": None,
            },
        }
        validator.validate(desc1)

        # 2. Redacted descriptor
        desc2 = {
            "schema_version": "0.1",
            "evidence_id": generate_evidence_id(),
            "sha256": compute_sha256(b"redacted data"),
            "size_bytes": 13,
            "mime_type": "application/json",
            "source": "openai",
            "account": "team-sec",
            "created_at": "2026-10-26T12:05:00Z",
            "collector_version": "1.0.0",
            "capture_point": "post_redaction",
            "redaction": {
                "redacted": True,
                "redaction_version": "v1.0.0",
            },
        }
        validator.validate(desc2)


if __name__ == "__main__":
    unittest.main()
