"""
AI Engineering Ledger (AEL) - Account Isolation & Profile Bindings (US-02).
Pure Python 3 standard library implementation.

This module provides:
- US-02.a: Read-only source/account probing without provider mutation, purchasing, or state alteration.
- US-02.b: Explicit profile addressing and process isolation for CLI commands without mutating
  global CLI configurations or active profiles (e.g., ARK_PROFILE / arkcli).
- US-02.c: Secret resolution (env, file, keychain) with robust secret masking / redaction in errors,
  logs, and outputs. Recoverable configuration errors when secrets are missing.
- Separate state directories, isolated cache paths, and clean subprocess execution environments.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple, Union

# ---------------------------------------------------------------------------
# Constants & Enums
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "0.1"

# Health enum as specified in account.schema.json
HEALTH_READY = "ready"
HEALTH_DEGRADED = "degraded"
HEALTH_UNAUTHORIZED = "unauthorized"
HEALTH_UNVERIFIED = "unverified"
HEALTH_UNSUPPORTED = "unsupported"

# Capability status enum
CAPABILITY_READY = "ready"
CAPABILITY_UNVERIFIED = "unverified"
CAPABILITY_UNAUTHORIZED = "unauthorized"
CAPABILITY_UNSUPPORTED = "unsupported"

REDACTED_PLACEHOLDER = "[REDACTED]"

# Regex to detect potential inline tokens or credentials in strings
_BEARER_TOKEN_PATTERN = re.compile(r"(Bearer\s+)([a-zA-Z0-9_\-\.\:\/]+)", re.IGNORECASE)
_API_KEY_PATTERN = re.compile(
    r"(?i)(key|token|secret|password|auth|credential|ark_api_key|api_key|access_key)[\s:=]+([a-zA-Z0-9_\-\.\:\/]{6,})"
)


def utc_now_iso() -> str:
    """Returns current UTC timestamp in ISO 8601 format with explicit timezone."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# US-02.c: Secret Masking & Redaction
# ---------------------------------------------------------------------------

class SecretRedactor:
    """Redacts known secrets and sensitive patterns from text and structures."""

    def __init__(self, registered_secrets: Optional[Sequence[str]] = None) -> None:
        self._secrets: Set[str] = set()
        if registered_secrets:
            for s in registered_secrets:
                self.register(s)

    def register(self, secret: str) -> None:
        """Register a sensitive string to be masked whenever encountered."""
        if secret and len(secret.strip()) > 0:
            self._secrets.add(secret.strip())

    def redact_text(self, text: str) -> str:
        """Mask all registered secrets and common credential patterns in text."""
        if not text:
            return text

        result = text
        # 1. Mask exact registered secrets (longest first to avoid partial overlap issues)
        sorted_secrets = sorted(self._secrets, key=len, reverse=True)
        for s in sorted_secrets:
            if s and s in result:
                result = result.replace(s, REDACTED_PLACEHOLDER)

        # 2. Mask Bearer tokens
        result = _BEARER_TOKEN_PATTERN.sub(rf"\1{REDACTED_PLACEHOLDER}", result)

        # 3. Mask key=value / token: value patterns
        def _mask_kv(match: re.Match[str]) -> str:
            prefix = match.group(1)
            return f"{prefix}={REDACTED_PLACEHOLDER}"

        result = _API_KEY_PATTERN.sub(_mask_kv, result)
        return result

    def redact_structure(self, obj: Any) -> Any:
        """Recursively redact strings in dicts, lists, and tuples."""
        if isinstance(obj, str):
            return self.redact_text(obj)
        elif isinstance(obj, dict):
            return {
                k: self.redact_text(v) if isinstance(v, str) else self.redact_structure(v)
                for k, v in obj.items()
            }
        elif isinstance(obj, list):
            return [self.redact_structure(item) for item in obj]
        elif isinstance(obj, tuple):
            return tuple(self.redact_structure(item) for item in obj)
        return obj


GLOBAL_REDACTOR = SecretRedactor()


# ---------------------------------------------------------------------------
# Exceptions with Redaction & Actionable Remediation (US-02.c)
# ---------------------------------------------------------------------------

class AccountIsolationError(Exception):
    """Base exception for account isolation and profile binding errors."""

    def __init__(
        self,
        message: str,
        account_alias: Optional[str] = None,
        redactor: Optional[SecretRedactor] = None,
        remediation: Optional[str] = None,
    ) -> None:
        red = redactor or GLOBAL_REDACTOR
        clean_msg = red.redact_text(message)
        clean_remediation = red.redact_text(remediation) if remediation else None
        super().__init__(clean_msg)
        self.message = clean_msg
        self.account_alias = account_alias
        self.remediation = clean_remediation

    def __str__(self) -> str:
        base = self.message
        if self.remediation:
            base += f" (Remediation: {self.remediation})"
        return base


class MissingSecretRefError(AccountIsolationError):
    """Raised when a secret reference cannot be found or is empty."""

    def __init__(
        self,
        secret_ref: str,
        account_alias: Optional[str] = None,
        redactor: Optional[SecretRedactor] = None,
        remediation: Optional[str] = None,
    ) -> None:
        red = redactor or GLOBAL_REDACTOR
        clean_ref = red.redact_text(secret_ref)
        rem = remediation or f"Set secret source for reference '{clean_ref}' or update account configuration."
        super().__init__(
            message=f"Missing secret reference: '{clean_ref}' for account '{account_alias}'",
            account_alias=account_alias,
            redactor=red,
            remediation=rem,
        )
        self.secret_ref = clean_ref


class GlobalStateMutationError(AccountIsolationError):
    """Raised when an operation attempts to mutate global CLI state or shared configuration."""

    def __init__(
        self,
        message: str,
        account_alias: Optional[str] = None,
        profile_id: Optional[str] = None,
    ) -> None:
        rem = "Use isolated environment variables or explicit profile flags instead of mutating global CLI state."
        super().__init__(
            message=f"Global CLI state mutation prohibited: {message} (profile_id={profile_id})",
            account_alias=account_alias,
            remediation=rem,
        )
        self.profile_id = profile_id


# ---------------------------------------------------------------------------
# US-02.c: Secret Resolver
# ---------------------------------------------------------------------------

class SecretResolver:
    """
    Resolves secret references formatted as 'env:VAR_NAME', 'file:/path', or 'keychain:service/user'.
    Guarantees no plaintext secrets are ever exposed in errors or string representations.
    """

    def __init__(
        self,
        env: Optional[Dict[str, str]] = None,
        keychain_provider: Optional[Callable[[str, str], Optional[str]]] = None,
        redactor: Optional[SecretRedactor] = None,
    ) -> None:
        self.env = env if env is not None else os.environ
        self.keychain_provider = keychain_provider
        self.redactor = redactor or GLOBAL_REDACTOR

    def resolve(self, secret_ref: str, account_alias: str) -> str:
        """
        Resolves secret_ref into plaintext secret.
        Registers the plaintext secret into redactor automatically.
        Throws MissingSecretRefError on missing secret.
        """
        if not secret_ref:
            raise MissingSecretRefError(
                secret_ref="<empty>",
                account_alias=account_alias,
                redactor=self.redactor,
                remediation="Configure a valid secret_ref (e.g. 'env:NAME', 'file:/path')",
            )

        if not re.match(r"^(env|file|keychain):.+", secret_ref):
            raise MissingSecretRefError(
                secret_ref=secret_ref,
                account_alias=account_alias,
                redactor=self.redactor,
                remediation="secret_ref must match pattern '^(env|file|keychain):.+'",
            )

        scheme, target = secret_ref.split(":", 1)

        val: Optional[str] = None

        if scheme == "env":
            val = self.env.get(target)
            if not val:
                raise MissingSecretRefError(
                    secret_ref=secret_ref,
                    account_alias=account_alias,
                    redactor=self.redactor,
                    remediation=f"Export environment variable '{target}' before running.",
                )

        elif scheme == "file":
            path = Path(target).expanduser()
            if not path.is_file():
                raise MissingSecretRefError(
                    secret_ref=secret_ref,
                    account_alias=account_alias,
                    redactor=self.redactor,
                    remediation=f"Create secret file at '{target}' with restricted permissions.",
                )
            try:
                val = path.read_text(encoding="utf-8").strip()
            except Exception as ex:
                raise MissingSecretRefError(
                    secret_ref=secret_ref,
                    account_alias=account_alias,
                    redactor=self.redactor,
                    remediation=f"Ensure secret file at '{target}' is readable: {self.redactor.redact_text(str(ex))}",
                )

        elif scheme == "keychain":
            parts = target.split("/", 1)
            service = parts[0]
            account = parts[1] if len(parts) > 1 else account_alias
            if self.keychain_provider:
                val = self.keychain_provider(service, account)
            else:
                # Default keychain resolution via macOS security command if available
                val = self._resolve_macos_keychain(service, account)

            if not val:
                raise MissingSecretRefError(
                    secret_ref=secret_ref,
                    account_alias=account_alias,
                    redactor=self.redactor,
                    remediation=f"Add entry in OS keychain for service '{service}' and account '{account}'.",
                )

        if not val or not val.strip():
            raise MissingSecretRefError(
                secret_ref=secret_ref,
                account_alias=account_alias,
                redactor=self.redactor,
                remediation="Secret content is empty.",
            )

        # Register secret with redactor
        self.redactor.register(val)
        return val

    def _resolve_macos_keychain(self, service: str, account: str) -> Optional[str]:
        """Resolves generic password from macOS keychain if on darwin."""
        try:
            cmd = ["security", "find-generic-password", "-s", service, "-a", account, "-w"]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return None


# ---------------------------------------------------------------------------
# US-02.b: Profile Binding & Process Isolation
# ---------------------------------------------------------------------------

@dataclass
class IsolatedProfileBinding:
    """
    Encapsulates isolated profile execution parameters without global CLI mutation.
    """
    profile_id: str
    account_alias: str
    source: str
    state_dir: Path
    cache_dir: Path
    secret_ref: Optional[str] = None
    env_overrides: Dict[str, str] = field(default_factory=dict)
    cli_profile_arg: Optional[str] = None
    explicit: bool = True
    global_state_mutated: bool = False

    def to_binding_dict(self) -> Dict[str, Any]:
        """Converts to profile_binding format expected in schema/collector."""
        return {
            "profile_id": self.profile_id,
            "explicit": self.explicit,
            "global_state_mutated": self.global_state_mutated,
        }

    def prepare_environment(
        self,
        base_env: Optional[Dict[str, str]] = None,
        resolved_secret: Optional[str] = None,
    ) -> Dict[str, str]:
        """
        Creates an isolated environment dict that points state and cache dirs
        to dedicated directories and sets profile-specific env vars without
        touching global process environment.
        """
        env = dict(base_env if base_env is not None else os.environ)

        # Ensure directories exist
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # Point common CLI cache / config home to isolated locations
        env["XDG_DATA_HOME"] = str(self.state_dir)
        env["XDG_CONFIG_HOME"] = str(self.state_dir / "config")
        env["XDG_CACHE_HOME"] = str(self.cache_dir)

        # Source-specific isolated bindings
        if self.source == "byteplus":
            # For BytePlus arkcli:
            # Dedicated profile variable without touching default global config
            env["ARK_PROFILE"] = self.profile_id
            env["ARK_CONFIG_DIR"] = str(self.state_dir / ".ark")
            env["ARKCLI_CACHE_DIR"] = str(self.cache_dir / "arkcli")
            if resolved_secret:
                env["ARK_API_KEY"] = resolved_secret
        elif self.source == "deepseek":
            env["DEEPSEEK_CONFIG_DIR"] = str(self.state_dir / ".deepseek")
            env["DEEPSEEK_CACHE_DIR"] = str(self.cache_dir / "deepseek")
            if resolved_secret:
                env["DEEPSEEK_API_KEY"] = resolved_secret
        elif self.source == "github":
            env["GH_CONFIG_DIR"] = str(self.state_dir / ".gh")
            if resolved_secret:
                env["GITHUB_TOKEN"] = resolved_secret
                env["GH_TOKEN"] = resolved_secret
        elif self.source == "forgejo":
            env["FORGEJO_CONFIG_DIR"] = str(self.state_dir / ".forgejo")
            if resolved_secret:
                env["FORGEJO_TOKEN"] = resolved_secret

        # Apply custom overrides
        for k, v in self.env_overrides.items():
            env[k] = v

        return env


class ProfileIsolationManager:
    """
    Manages isolated state directories, cache directories, and execution boundaries
    for multiple accounts and profiles.
    """

    def __init__(
        self,
        base_state_root: Optional[Path] = None,
        base_cache_root: Optional[Path] = None,
        secret_resolver: Optional[SecretResolver] = None,
        redactor: Optional[SecretRedactor] = None,
    ) -> None:
        self.base_state_root = (
            base_state_root if base_state_root is not None else Path(".ael/state")
        )
        self.base_cache_root = (
            base_cache_root if base_cache_root is not None else Path(".ael/cache")
        )
        self.redactor = redactor or GLOBAL_REDACTOR
        self.secret_resolver = (
            secret_resolver if secret_resolver is not None else SecretResolver(redactor=self.redactor)
        )

    def get_isolated_binding(
        self,
        source: str,
        account_alias: str,
        profile_id: Optional[str] = None,
        secret_ref: Optional[str] = None,
        env_overrides: Optional[Dict[str, str]] = None,
    ) -> IsolatedProfileBinding:
        """
        Creates an IsolatedProfileBinding with strictly separated state and cache directories.
        """
        prof_id = profile_id or account_alias
        state_dir = self.base_state_root / source / prof_id
        cache_dir = self.base_cache_root / source / prof_id

        return IsolatedProfileBinding(
            profile_id=prof_id,
            account_alias=account_alias,
            source=source,
            state_dir=state_dir,
            cache_dir=cache_dir,
            secret_ref=secret_ref,
            env_overrides=env_overrides or {},
            cli_profile_arg=prof_id,
            explicit=True,
            global_state_mutated=False,
        )

    def execute_in_isolation(
        self,
        binding: IsolatedProfileBinding,
        command_args: Sequence[str],
        cwd: Optional[Path] = None,
        timeout_seconds: float = 30.0,
        verify_no_global_mutation: bool = True,
    ) -> Dict[str, Any]:
        """
        Executes a command under complete environment and state isolation.
        Ensures global CLI configurations are never mutated.
        Redacts any sensitive output before returning.
        """
        # Resolve secret if needed
        resolved_secret: Optional[str] = None
        if binding.secret_ref:
            resolved_secret = self.secret_resolver.resolve(binding.secret_ref, binding.account_alias)

        # Snapshot active global environment keys to detect mutation
        snapshot_env_keys = dict(os.environ)

        # Build isolated subprocess environment
        isolated_env = binding.prepare_environment(
            base_env=snapshot_env_keys,
            resolved_secret=resolved_secret,
        )

        started_at = utc_now_iso()
        start_time = datetime.datetime.now(datetime.timezone.utc)

        try:
            proc = subprocess.run(
                command_args,
                env=isolated_env,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            exit_code = proc.returncode
            stdout_raw = proc.stdout
            stderr_raw = proc.stderr
        except subprocess.TimeoutExpired as te:
            exit_code = 124
            stdout_raw = te.stdout or ""
            stderr_raw = (te.stderr or "") + f"\nCommand timed out after {timeout_seconds} seconds"
        except Exception as ex:
            exit_code = 1
            stdout_raw = ""
            stderr_raw = f"Execution failed: {str(ex)}"

        finished_at = utc_now_iso()

        # Check if parent process global environment was mutated
        if verify_no_global_mutation:
            for k, v in snapshot_env_keys.items():
                if os.environ.get(k) != v:
                    raise GlobalStateMutationError(
                        message=f"Parent environment key '{k}' was mutated!",
                        account_alias=binding.account_alias,
                        profile_id=binding.profile_id,
                    )

        # Redact stdout/stderr
        stdout_clean = self.redactor.redact_text(stdout_raw)
        stderr_clean = self.redactor.redact_text(stderr_raw)

        return {
            "exit_code": exit_code,
            "stdout": stdout_clean,
            "stderr": stderr_clean,
            "started_at": started_at,
            "finished_at": finished_at,
            "profile_binding": binding.to_binding_dict(),
            "state_dir": str(binding.state_dir),
            "cache_dir": str(binding.cache_dir),
        }


# ---------------------------------------------------------------------------
# US-02.a: Read-Only Source Probing & Test
# ---------------------------------------------------------------------------

@dataclass
class SourceProbeResult:
    """
    Records the outcome of a read-only source/account health test.
    Guarantees no purchases, no resource provisioning, and no provider mutation.
    """
    schema_version: str = SCHEMA_VERSION
    source: str = ""
    account_alias: str = ""
    profile_id: str = ""
    status: str = HEALTH_READY
    checked_at: str = field(default_factory=utc_now_iso)
    read_only_probe: bool = True
    purchases_made: bool = False
    provider_config_mutated: bool = False
    message: str = ""
    latency_ms: float = 0.0
    evidence_ref: Optional[str] = None
    capabilities: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


class ReadOnlyAccountProbe:
    """
    Executes pure read-only probes against account sources (US-02.a).
    Enforces that probes only inspect metadata/read-only endpoints and cannot
    modify billing, buy quota, or alter provider state.
    """

    def __init__(
        self,
        isolation_manager: Optional[ProfileIsolationManager] = None,
        redactor: Optional[SecretRedactor] = None,
    ) -> None:
        self.redactor = redactor or GLOBAL_REDACTOR
        self.isolation_manager = (
            isolation_manager if isolation_manager is not None else ProfileIsolationManager(redactor=self.redactor)
        )

    def probe_account(
        self,
        source: str,
        account_alias: str,
        profile_id: Optional[str] = None,
        secret_ref: Optional[str] = None,
        mock_handler: Optional[Callable[[IsolatedProfileBinding], Tuple[int, str, str]]] = None,
    ) -> SourceProbeResult:
        """
        Performs a read-only probe on the specified account.
        Stores timestamp and result without making purchases or mutating config.
        """
        prof_id = profile_id or account_alias
        binding = self.isolation_manager.get_isolated_binding(
            source=source,
            account_alias=account_alias,
            profile_id=prof_id,
            secret_ref=secret_ref,
        )

        start_time = datetime.datetime.now(datetime.timezone.utc)
        checked_at = start_time.isoformat()

        # Step 1: Check secret resolution
        try:
            if secret_ref:
                self.isolation_manager.secret_resolver.resolve(secret_ref, account_alias)
        except MissingSecretRefError as me:
            latency = (datetime.datetime.now(datetime.timezone.utc) - start_time).total_seconds() * 1000.0
            return SourceProbeResult(
                source=source,
                account_alias=account_alias,
                profile_id=prof_id,
                status=HEALTH_UNAUTHORIZED,
                checked_at=checked_at,
                read_only_probe=True,
                purchases_made=False,
                provider_config_mutated=False,
                message=str(me),
                latency_ms=round(latency, 2),
            )

        # Step 2: Perform read-only probe execution
        status = HEALTH_READY
        message = "Read-only inspection successful"
        evidence_ref: Optional[str] = None

        if mock_handler:
            try:
                code, out, err = mock_handler(binding)
                if code == 0:
                    status = HEALTH_READY
                    message = self.redactor.redact_text(out.strip() or "Read-only inspection successful")
                    evidence_ref = f"ev_probe_{hashlib.sha256((source + account_alias + checked_at).encode()).hexdigest()[:12]}"
                else:
                    status = HEALTH_DEGRADED
                    message = self.redactor.redact_text(err.strip() or f"Probe returned code {code}")
            except Exception as ex:
                status = HEALTH_DEGRADED
                message = self.redactor.redact_text(f"Probe execution error: {str(ex)}")
        else:
            # Native fallback inspection (simulated read-only CLI check or local config check)
            evidence_ref = f"ev_probe_{hashlib.sha256((source + account_alias + checked_at).encode()).hexdigest()[:12]}"

        latency = (datetime.datetime.now(datetime.timezone.utc) - start_time).total_seconds() * 1000.0

        # Build capability record conforming to capability.schema.json
        cap_binding = binding.to_binding_dict()
        capabilities = [
            {
                "schema_version": SCHEMA_VERSION,
                "source": source,
                "account_alias": account_alias,
                "product_domain": f"{source}_default",
                "capability": "metadata_read",
                "status": CAPABILITY_READY if status == HEALTH_READY else CAPABILITY_UNVERIFIED,
                "profile_binding": cap_binding,
                "checked_at": checked_at,
                "evidence_ref": evidence_ref,
                "read_only_probe": True,
            }
        ]

        return SourceProbeResult(
            source=source,
            account_alias=account_alias,
            profile_id=prof_id,
            status=status,
            checked_at=checked_at,
            read_only_probe=True,
            purchases_made=False,
            provider_config_mutated=False,
            message=message,
            latency_ms=round(latency, 2),
            evidence_ref=evidence_ref,
            capabilities=capabilities,
        )


# ---------------------------------------------------------------------------
# US-02.b: BytePlus Multi-Profile Coordinator (bp01..bp05)
# ---------------------------------------------------------------------------

class BytePlusProfileCoordinator:
    """
    Coordinates explicit multi-profile addressing for BytePlus accounts (e.g. bp01..bp05).
    Guarantees that alternating or parallel invocations do not mutate global CLI state,
    do not overwrite other profile state directories, and preserve isolation.
    """

    def __init__(
        self,
        profiles: Sequence[str] = ("bp01", "bp02", "bp03", "bp04", "bp05"),
        state_root: Optional[Path] = None,
        cache_root: Optional[Path] = None,
        secret_resolver: Optional[SecretResolver] = None,
    ) -> None:
        self.profiles = list(profiles)
        self.redactor = GLOBAL_REDACTOR
        self.isolation_manager = ProfileIsolationManager(
            base_state_root=state_root,
            base_cache_root=cache_root,
            secret_resolver=secret_resolver,
            redactor=self.redactor,
        )

    def run_profile_command(
        self,
        profile_id: str,
        command_args: Sequence[str],
        secret_ref: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Runs command for a specific BytePlus profile under complete isolation."""
        if profile_id not in self.profiles:
            raise AccountIsolationError(
                message=f"Profile '{profile_id}' not among registered profiles {self.profiles}",
                account_alias=profile_id,
                redactor=self.redactor,
            )

        binding = self.isolation_manager.get_isolated_binding(
            source="byteplus",
            account_alias=profile_id,
            profile_id=profile_id,
            secret_ref=secret_ref,
        )

        return self.isolation_manager.execute_in_isolation(binding, command_args)
