"""Minimal dependency-free adapter demonstrating the v0.4 conformance lifecycle."""
from __future__ import annotations

import hashlib
from typing import Any


class ExampleAdapter:
    source = "example"
    manifest = {
        "schema_version": "0.4",
        "adapter_id": "example",
        "source": "example",
        "surface": "fixture",
        "capabilities": ["historical"],
        "checkpoint_semantics": "stable source row id",
        "evidence_states": ["value", "unknown", "unsupported"],
    }

    def capabilities(self) -> list[dict[str, Any]]:
        return [{"capability": "historical", "status": "ready", "read_only_probe": True}]

    def validate(self, account: dict[str, Any]) -> None:
        if not account.get("alias") or "token" in account:
            raise ValueError("account requires alias and must use an external secret_ref")

    def collect(self, account: dict[str, Any], payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.validate(account)
        return payload

    def normalize(self, account: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{
            "source": self.source, "account_alias": account["alias"], "metric": row["metric"],
            "value": row["value"], "unit": row["unit"],
            "idempotency_key": hashlib.sha256(
                f"{self.source}:{account['alias']}:{row['id']}".encode()
            ).hexdigest(),
        } for row in rows]
