"""
Configuration schema, loader, and source activation for AI Engineering Ledger (AEL).
Pure Python 3 standard library implementation.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# Supported source types as specified in PRD section 6 & US-01.b
SUPPORTED_SOURCES: Set[str] = {
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

# Capability status enum values as defined in US-01.c and JSON schema
STATUS_UNSUPPORTED = "unsupported"
STATUS_UNVERIFIED = "unverified"
STATUS_UNAUTHORIZED = "unauthorized"
STATUS_READY = "ready"

SUPPORTED_STATUSES: Set[str] = {
    STATUS_UNSUPPORTED,
    STATUS_UNVERIFIED,
    STATUS_UNAUTHORIZED,
    STATUS_READY,
}

# Health status enum values as per schema/collector/account.schema.json
HEALTH_READY = "ready"
HEALTH_DEGRADED = "degraded"
HEALTH_UNAUTHORIZED = "unauthorized"
HEALTH_UNVERIFIED = "unverified"
HEALTH_UNSUPPORTED = "unsupported"

SUPPORTED_HEALTHS: Set[str] = {
    HEALTH_READY,
    HEALTH_DEGRADED,
    HEALTH_UNAUTHORIZED,
    HEALTH_UNVERIFIED,
    HEALTH_UNSUPPORTED,
}

# Supported product domains
SUPPORTED_PRODUCT_DOMAINS: Set[str] = {
    "byteplus_coding_plan",
    "byteplus_inference",
    "byteplus_billing",
    "subscription",
    "api",
    "runtime",
    "git",
    "manual",
}

# Regex for alias / identifier validation
ALIAS_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SECRET_REF_PATTERN = re.compile(r"^(env|keychain|file):.+")
TIMESTAMP_PATTERN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})$")


class ConfigSchemaError(ValueError):
    """Raised when configuration validation or migration fails."""

    def __init__(self, message: str, field: Optional[str] = None, migration_guide: Optional[str] = None):
        super().__init__(message)
        self.field = field
        self.migration_guide = migration_guide


def utc_now_iso() -> str:
    """Return current timestamp formatted as ISO8601 UTC with Z suffix."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class ProfileBinding:
    profile_id: str
    explicit: bool = True
    global_state_mutated: bool = False

    def validate(self, parent_account: str) -> None:
        if not ALIAS_PATTERN.match(self.profile_id):
            raise ConfigSchemaError(
                f"Invalid profile_id '{self.profile_id}' for account '{parent_account}'",
                field="profile_binding.profile_id"
            )
        if not self.explicit:
            raise ConfigSchemaError(
                f"Profile binding must be explicit (explicit=True) for account '{parent_account}'",
                field="profile_binding.explicit"
            )
        if self.global_state_mutated:
            raise ConfigSchemaError(
                f"Profile binding must not mutate global CLI/SDK state (global_state_mutated=False) for account '{parent_account}'",
                field="profile_binding.global_state_mutated"
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "explicit": self.explicit,
            "global_state_mutated": self.global_state_mutated,
        }


@dataclass
class Capability:
    schema_version: str
    source: str
    account_alias: str
    product_domain: str
    capability: str
    status: str
    profile_binding: ProfileBinding
    checked_at: str
    read_only_probe: Optional[bool] = None
    evidence_ref: Optional[str] = None
    reason: Optional[str] = None

    def validate(self) -> None:
        if self.schema_version != "0.1":
            raise ConfigSchemaError(
                f"Unsupported capability schema version '{self.schema_version}'. Supported: '0.1'.",
                field="capability.schema_version",
                migration_guide="docs/migration-v0.1.md"
            )
        if self.source not in SUPPORTED_SOURCES:
            raise ConfigSchemaError(
                f"Unsupported source '{self.source}' in capability",
                field="capability.source"
            )
        if not ALIAS_PATTERN.match(self.account_alias):
            raise ConfigSchemaError(
                f"Invalid account_alias '{self.account_alias}' in capability",
                field="capability.account_alias"
            )
        if self.status not in SUPPORTED_STATUSES:
            raise ConfigSchemaError(
                f"Invalid capability status '{self.status}'. Must be one of {sorted(SUPPORTED_STATUSES)}",
                field="capability.status"
            )
        if not TIMESTAMP_PATTERN.match(self.checked_at):
            raise ConfigSchemaError(
                f"Invalid checked_at timestamp '{self.checked_at}'",
                field="capability.checked_at"
            )

        self.profile_binding.validate(self.account_alias)

        if self.status in {STATUS_READY, STATUS_UNAUTHORIZED}:
            if self.read_only_probe is not True:
                raise ConfigSchemaError(
                    f"Capability with status '{self.status}' requires read_only_probe=True",
                    field="capability.read_only_probe"
                )
            if not self.evidence_ref:
                raise ConfigSchemaError(
                    f"Capability with status '{self.status}' requires evidence_ref",
                    field="capability.evidence_ref"
                )
        elif self.status in {STATUS_UNSUPPORTED, STATUS_UNVERIFIED}:
            if not self.reason:
                raise ConfigSchemaError(
                    f"Capability with status '{self.status}' requires reason",
                    field="capability.reason"
                )

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "schema_version": self.schema_version,
            "source": self.source,
            "account_alias": self.account_alias,
            "product_domain": self.product_domain,
            "capability": self.capability,
            "status": self.status,
            "profile_binding": self.profile_binding.to_dict(),
            "checked_at": self.checked_at,
        }
        if self.read_only_probe is not None:
            data["read_only_probe"] = self.read_only_probe
        if self.evidence_ref is not None:
            data["evidence_ref"] = self.evidence_ref
        if self.reason is not None:
            data["reason"] = self.reason
        return data


@dataclass
class Account:
    schema_version: str
    source: str
    alias: str
    profile_id: str
    product_domains: List[str]
    capabilities: List[Capability] = field(default_factory=list)
    health: str = HEALTH_UNVERIFIED
    health_checked_at: Optional[str] = None
    secret_ref: Optional[str] = None

    def validate(self) -> None:
        if self.schema_version != "0.1":
            raise ConfigSchemaError(
                f"Unsupported account schema version '{self.schema_version}'. Supported: '0.1'.",
                field="account.schema_version",
                migration_guide="docs/migration-v0.1.md"
            )
        if self.source not in SUPPORTED_SOURCES:
            raise ConfigSchemaError(
                f"Unsupported source '{self.source}' in account '{self.alias}'",
                field="account.source"
            )
        if not ALIAS_PATTERN.match(self.alias):
            raise ConfigSchemaError(
                f"Invalid account alias '{self.alias}'. Must match pattern {ALIAS_PATTERN.pattern}",
                field="account.alias"
            )
        if not ALIAS_PATTERN.match(self.profile_id):
            raise ConfigSchemaError(
                f"Invalid profile_id '{self.profile_id}' for account '{self.alias}'",
                field="account.profile_id"
            )
        if not self.product_domains:
            raise ConfigSchemaError(
                f"Account '{self.alias}' must define at least one product_domain",
                field="account.product_domains"
            )
        for pd in self.product_domains:
            if pd not in SUPPORTED_PRODUCT_DOMAINS:
                raise ConfigSchemaError(
                    f"Unsupported product domain '{pd}' in account '{self.alias}'",
                    field="account.product_domains"
                )
        if self.health not in SUPPORTED_HEALTHS:
            raise ConfigSchemaError(
                f"Invalid account health '{self.health}' in account '{self.alias}'",
                field="account.health"
            )
        if self.health_checked_at and not TIMESTAMP_PATTERN.match(self.health_checked_at):
            raise ConfigSchemaError(
                f"Invalid health_checked_at timestamp '{self.health_checked_at}' in account '{self.alias}'",
                field="account.health_checked_at"
            )
        if self.secret_ref and not SECRET_REF_PATTERN.match(self.secret_ref):
            raise ConfigSchemaError(
                f"Invalid secret_ref '{self.secret_ref}' in account '{self.alias}'. Must match ^(env|keychain|file):.+",
                field="account.secret_ref"
            )

        for cap in self.capabilities:
            cap.validate()

    def update_health(self, new_health: str, checked_at: Optional[str] = None) -> None:
        if new_health not in SUPPORTED_HEALTHS:
            raise ConfigSchemaError(
                f"Invalid health status '{new_health}'. Must be one of {sorted(SUPPORTED_HEALTHS)}",
                field="account.health"
            )
        self.health = new_health
        self.health_checked_at = checked_at or utc_now_iso()

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "schema_version": self.schema_version,
            "source": self.source,
            "alias": self.alias,
            "profile_id": self.profile_id,
            "product_domains": list(self.product_domains),
            "capabilities": [c.to_dict() for c in self.capabilities],
            "health": self.health,
        }
        if self.health_checked_at:
            data["health_checked_at"] = self.health_checked_at
        if self.secret_ref:
            data["secret_ref"] = self.secret_ref
        return data


@dataclass
class SourceConfig:
    source_type: str
    name: str
    enabled: bool = True
    collection_methods: List[str] = field(default_factory=list)
    accounts: Dict[str, Account] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)

    def validate(self) -> None:
        if self.source_type not in SUPPORTED_SOURCES:
            raise ConfigSchemaError(
                f"Source type '{self.source_type}' is unsupported. Must be one of {sorted(SUPPORTED_SOURCES)}",
                field="source.source_type"
            )
        if not self.name:
            raise ConfigSchemaError("Source name cannot be empty", field="source.name")

        for alias, acc in self.accounts.items():
            if alias != acc.alias:
                raise ConfigSchemaError(
                    f"Account alias key mismatch: '{alias}' vs '{acc.alias}'",
                    field="source.accounts"
                )
            if acc.source != self.source_type:
                raise ConfigSchemaError(
                    f"Account source mismatch: expected '{self.source_type}', got '{acc.source}'",
                    field="account.source"
                )
            acc.validate()

    def add_account(self, account: Account) -> None:
        if account.source != self.source_type:
            raise ConfigSchemaError(
                f"Cannot add account with source '{account.source}' to source '{self.source_type}'",
                field="account.source"
            )
        account.validate()
        self.accounts[account.alias] = account

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_type": self.source_type,
            "name": self.name,
            "enabled": self.enabled,
            "collection_methods": list(self.collection_methods),
            "accounts": {k: v.to_dict() for k, v in sorted(self.accounts.items())},
            "created_at": self.created_at,
        }


@dataclass
class AELConfig:
    schema_version: str = "0.1"
    sources: Dict[str, SourceConfig] = field(default_factory=dict)
    initialized_at: str = field(default_factory=utc_now_iso)

    def validate(self) -> None:
        # US-01.d: Schema-Hauptversion check
        major_version = self.schema_version.split(".")[0]
        if major_version != "0":
            raise ConfigSchemaError(
                f"Unknown schema major version '{self.schema_version}'. Supported major version is '0.x'.",
                field="schema_version",
                migration_guide="docs/migration-v0.1.md"
            )

        for src_type, src in self.sources.items():
            if src_type != src.source_type:
                raise ConfigSchemaError(
                    f"Source type key mismatch: '{src_type}' vs '{src.source_type}'",
                    field="sources"
                )
            src.validate()

    def register_source(
        self,
        source_type: str,
        name: Optional[str] = None,
        enabled: bool = True,
        collection_methods: Optional[List[str]] = None,
    ) -> SourceConfig:
        """Register or update a source type (US-01.b: 9 source types supported)."""
        if source_type not in SUPPORTED_SOURCES:
            raise ConfigSchemaError(
                f"Unsupported source type '{source_type}'. Supported: {sorted(SUPPORTED_SOURCES)}",
                field="sources.source_type"
            )

        if source_type in self.sources:
            src = self.sources[source_type]
            src.enabled = enabled
            if name:
                src.name = name
            if collection_methods is not None:
                src.collection_methods = list(collection_methods)
            return src

        default_name = name or f"{source_type.title()} Source"
        default_methods = collection_methods or default_collection_methods_for(source_type)
        src = SourceConfig(
            source_type=source_type,
            name=default_name,
            enabled=enabled,
            collection_methods=default_methods,
        )
        src.validate()
        self.sources[source_type] = src
        return src

    def add_account(
        self,
        source_type: str,
        alias: str,
        profile_id: Optional[str] = None,
        product_domains: Optional[List[str]] = None,
        secret_ref: Optional[str] = None,
        health: str = HEALTH_UNVERIFIED,
    ) -> Account:
        """Add an account alias to a source (US-01.b: arbitrary number of aliases)."""
        if source_type not in self.sources:
            self.register_source(source_type)

        src = self.sources[source_type]
        p_id = profile_id or alias
        p_domains = product_domains or default_product_domains_for(source_type)

        account = Account(
            schema_version="0.1",
            source=source_type,
            alias=alias,
            profile_id=p_id,
            product_domains=p_domains,
            health=health,
            secret_ref=secret_ref,
        )
        src.add_account(account)
        return account

    def get_source_account_entries(self) -> List[Dict[str, Any]]:
        """
        List all source, account, collection method, and capability entries (US-01.c).
        Distinguishes unsupported, unverified, unauthorized, and ready.
        """
        entries: List[Dict[str, Any]] = []
        for src_type, src in sorted(self.sources.items()):
            if not src.accounts:
                entries.append({
                    "source": src_type,
                    "account": None,
                    "collection_methods": list(src.collection_methods),
                    "enabled": src.enabled,
                    "health": None,
                    "capabilities": [],
                })
            else:
                for alias, acc in sorted(src.accounts.items()):
                    caps = []
                    for c in acc.capabilities:
                        caps.append({
                            "capability": c.capability,
                            "product_domain": c.product_domain,
                            "status": c.status,
                            "reason": c.reason,
                            "evidence_ref": c.evidence_ref,
                        })
                    entries.append({
                        "source": src_type,
                        "account": alias,
                        "profile_id": acc.profile_id,
                        "collection_methods": list(src.collection_methods),
                        "enabled": src.enabled,
                        "health": acc.health,
                        "health_checked_at": acc.health_checked_at,
                        "capabilities": caps,
                    })
        return entries

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "initialized_at": self.initialized_at,
            "sources": {k: v.to_dict() for k, v in sorted(self.sources.items())},
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> AELConfig:
        if not isinstance(data, dict):
            raise ConfigSchemaError("Configuration root must be a JSON object", field="root")

        schema_version = data.get("schema_version", "0.1")
        major_version = str(schema_version).split(".")[0]
        if major_version != "0":
            raise ConfigSchemaError(
                f"Unknown schema major version '{schema_version}'. Supported major version is '0.x'.",
                field="schema_version",
                migration_guide="docs/migration-v0.1.md"
            )

        initialized_at = data.get("initialized_at", utc_now_iso())
        sources_dict: Dict[str, SourceConfig] = {}

        raw_sources = data.get("sources", {})
        if not isinstance(raw_sources, dict):
            raise ConfigSchemaError("Field 'sources' must be an object", field="sources")

        for src_type, src_raw in raw_sources.items():
            if not isinstance(src_raw, dict):
                raise ConfigSchemaError(f"Source '{src_type}' configuration must be an object", field=f"sources.{src_type}")

            accounts_dict: Dict[str, Account] = {}
            for acc_alias, acc_raw in src_raw.get("accounts", {}).items():
                caps_list: List[Capability] = []
                for cap_raw in acc_raw.get("capabilities", []):
                    pb_raw = cap_raw.get("profile_binding", {})
                    pb = ProfileBinding(
                        profile_id=pb_raw.get("profile_id", acc_raw.get("profile_id", acc_alias)),
                        explicit=pb_raw.get("explicit", True),
                        global_state_mutated=pb_raw.get("global_state_mutated", False),
                    )
                    caps_list.append(
                        Capability(
                            schema_version=cap_raw.get("schema_version", "0.1"),
                            source=cap_raw.get("source", src_type),
                            account_alias=cap_raw.get("account_alias", acc_alias),
                            product_domain=cap_raw.get("product_domain", "api"),
                            capability=cap_raw.get("capability", "usage"),
                            status=cap_raw.get("status", STATUS_UNVERIFIED),
                            profile_binding=pb,
                            checked_at=cap_raw.get("checked_at", utc_now_iso()),
                            read_only_probe=cap_raw.get("read_only_probe"),
                            evidence_ref=cap_raw.get("evidence_ref"),
                            reason=cap_raw.get("reason"),
                        )
                    )

                acc = Account(
                    schema_version=acc_raw.get("schema_version", "0.1"),
                    source=acc_raw.get("source", src_type),
                    alias=acc_alias,
                    profile_id=acc_raw.get("profile_id", acc_alias),
                    product_domains=acc_raw.get("product_domains", default_product_domains_for(src_type)),
                    capabilities=caps_list,
                    health=acc_raw.get("health", HEALTH_UNVERIFIED),
                    health_checked_at=acc_raw.get("health_checked_at"),
                    secret_ref=acc_raw.get("secret_ref"),
                )
                accounts_dict[acc_alias] = acc

            src = SourceConfig(
                source_type=src_type,
                name=src_raw.get("name", f"{src_type.title()} Source"),
                enabled=src_raw.get("enabled", True),
                collection_methods=src_raw.get("collection_methods", default_collection_methods_for(src_type)),
                accounts=accounts_dict,
                created_at=src_raw.get("created_at", utc_now_iso()),
            )
            sources_dict[src_type] = src

        cfg = cls(
            schema_version=schema_version,
            sources=sources_dict,
            initialized_at=initialized_at,
        )
        cfg.validate()
        return cfg


def default_collection_methods_for(source_type: str) -> List[str]:
    """Default collection methods according to PRD section 6 Quellenmatrix."""
    mapping = {
        "deepseek": ["import_csv", "import_json", "api_poll"],
        "byteplus": ["ark_cli", "quota_snapshot", "import_usage"],
        "anthropic": ["manual_snapshot", "import_json"],
        "openai": ["manual_snapshot", "import_json"],
        "google": ["manual_snapshot", "credit_history"],
        "kilo": ["import_session_json", "otlp_receiver"],
        "faigate": ["export_import", "cron_import"],
        "github": ["rest_api", "git_log"],
        "forgejo": ["rest_api", "git_log"],
    }
    return mapping.get(source_type, ["import_json", "manual_snapshot"])


def default_product_domains_for(source_type: str) -> List[str]:
    """Default product domain according to PRD & schema."""
    mapping = {
        "deepseek": ["api"],
        "byteplus": ["byteplus_coding_plan", "byteplus_inference", "byteplus_billing"],
        "anthropic": ["subscription", "manual"],
        "openai": ["subscription", "manual"],
        "google": ["subscription", "manual"],
        "kilo": ["runtime"],
        "faigate": ["runtime", "api"],
        "github": ["git"],
        "forgejo": ["git"],
    }
    return mapping.get(source_type, ["api"])


class ConfigManager:
    """Manages initialization, loading, saving, status and offline help for AEL ledger configuration."""

    DEFAULT_CONFIG_PATH = Path(".ael/config.json")

    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = Path(config_path) if config_path else self.DEFAULT_CONFIG_PATH

    def initialize(self, force: bool = False) -> Tuple[AELConfig, bool]:
        """
        US-01.a: Initialize an empty configuration.
        Re-initialization preserves existing data (does not overwrite unless forced).
        Returns (config, created_new: bool).
        """
        if self.config_path.exists() and not force:
            # Preserve existing data!
            existing = self.load()
            return existing, False

        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        config = AELConfig()
        self.save(config)
        return config, True

    def load(self) -> AELConfig:
        """Load and validate configuration from disk."""
        if not self.config_path.exists():
            raise FileNotFoundError(f"Configuration file not found at {self.config_path}. Run 'init' first.")
        content = self.config_path.read_text(encoding="utf-8")
        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            raise ConfigSchemaError(f"Malformed JSON in config file: {e}", field="json_syntax")
        return AELConfig.from_dict(data)

    def save(self, config: AELConfig) -> None:
        """Validate and write configuration to disk."""
        config.validate()
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(config.to_dict(), indent=2, ensure_ascii=False) + "\n"
        self.config_path.write_text(serialized, encoding="utf-8")

    def status(self) -> Dict[str, Any]:
        """
        US-01.a: Return status offline without any network requirement.
        """
        is_initialized = self.config_path.exists()
        if not is_initialized:
            return {
                "initialized": False,
                "config_path": str(self.config_path),
                "network_required": False,
                "sources_total": 0,
                "sources_enabled": 0,
                "accounts_total": 0,
                "health_summary": {},
            }

        config = self.load()
        sources_total = len(config.sources)
        sources_enabled = sum(1 for s in config.sources.values() if s.enabled)
        accounts_total = sum(len(s.accounts) for s in config.sources.values())

        health_counts: Dict[str, int] = {}
        for s in config.sources.values():
            for acc in s.accounts.values():
                health_counts[acc.health] = health_counts.get(acc.health, 0) + 1

        return {
            "initialized": True,
            "config_path": str(self.config_path),
            "schema_version": config.schema_version,
            "initialized_at": config.initialized_at,
            "network_required": False,
            "sources_total": sources_total,
            "sources_enabled": sources_enabled,
            "accounts_total": accounts_total,
            "health_summary": health_counts,
        }

    def help_offline(self) -> str:
        """
        US-01.a: Return comprehensive CLI and system help completely offline.
        """
        return """AI Engineering Ledger (AEL) - Offline Help
===================================================
AEL operates locally without requiring any cloud account or network connection.

Core Commands:
  init                     Initialize a local ledger configuration (.ael/config.json).
                           Safe to re-run; existing data is preserved.
  status                   Display ledger status, source counts, and account health.
  help                     Display this offline help.
  source list              List registered sources, accounts, and collection methods.
  source add <type>        Register a supported source type.
  account add <src> <al>   Add an account alias with isolated profile binding.
  capability check         Report status: unsupported, unverified, unauthorized, ready.

Supported Source Types (US-01.b):
  deepseek, byteplus, anthropic, openai, google, kilo, faigate, github, forgejo

Account Aliases:
  Arbitrary number of accounts may be attached per source (e.g. bp01..bp05).
  Each account maintains independent health and explicit profile isolation.
"""
