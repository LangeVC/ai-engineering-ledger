"""
Unit and integration tests for Account Isolation & Profile Bindings (US-02).
Pure Python 3 standard library (unittest).

Tests covering:
- US-02.a: Read-only source/account probing (does not buy anything, does not mutate provider config, stores timestamp & result).
- US-02.b: 5 BytePlus profiles (bp01..bp05) explicitly addressed, alternating executions do not mutate active global CLI account or shared directories.
- US-02.c: Missing secret reference produces recoverable error without leaking secrets; redacts secrets in messages/exceptions.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from collectors.account import (
    HEALTH_DEGRADED,
    HEALTH_READY,
    HEALTH_UNAUTHORIZED,
    REDACTED_PLACEHOLDER,
    AccountIsolationError,
    BytePlusProfileCoordinator,
    GlobalStateMutationError,
    IsolatedProfileBinding,
    MissingSecretRefError,
    ProfileIsolationManager,
    ReadOnlyAccountProbe,
    SecretRedactor,
    SecretResolver,
    SourceProbeResult,
)


class TestAccountIsolationUS02(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="ael_account_test_")
        self.state_root = Path(self.temp_dir) / "state"
        self.cache_root = Path(self.temp_dir) / "cache"

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # -----------------------------------------------------------------------
    # US-02.a: Read-Only Probe Tests
    # -----------------------------------------------------------------------
    def test_us02_a_source_test_reads_only_and_records_result_and_timestamp(self) -> None:
        """
        US-02.a: Ein Source-Test prüft nur Lesezugriff und speichert Ergebnis und
        Zeitpunkt; er kauft nichts und ändert keine Providerkonfiguration.
        """
        redactor = SecretRedactor()
        mgr = ProfileIsolationManager(
            base_state_root=self.state_root,
            base_cache_root=self.cache_root,
            redactor=redactor,
        )
        probe = ReadOnlyAccountProbe(isolation_manager=mgr, redactor=redactor)

        # Execute probe on an account
        result = probe.probe_account(
            source="byteplus",
            account_alias="bp01",
            profile_id="bp01",
        )

        self.assertIsInstance(result, SourceProbeResult)
        self.assertEqual(result.source, "byteplus")
        self.assertEqual(result.account_alias, "bp01")
        self.assertEqual(result.profile_id, "bp01")
        self.assertEqual(result.status, HEALTH_READY)
        self.assertTrue(result.read_only_probe)
        self.assertFalse(result.purchases_made)
        self.assertFalse(result.provider_config_mutated)

        # Verified timestamp presence
        self.assertTrue(len(result.checked_at) > 10)
        self.assertIn("T", result.checked_at)

        # Conforms to capability requirements
        self.assertEqual(len(result.capabilities), 1)
        cap = result.capabilities[0]
        self.assertEqual(cap["account_alias"], "bp01")
        self.assertTrue(cap["read_only_probe"])
        self.assertFalse(cap["profile_binding"]["global_state_mutated"])
        self.assertTrue(cap["profile_binding"]["explicit"])

    def test_us02_a_mock_probe_custom_handler(self) -> None:
        """Verify custom handler gets called with isolated binding and retains read-only guarantees."""
        probe = ReadOnlyAccountProbe()
        called_with: list[IsolatedProfileBinding] = []

        def mock_read_only_handler(binding: IsolatedProfileBinding) -> tuple[int, str, str]:
            called_with.append(binding)
            return 0, "Account balance: 100.00 (read-only query)", ""

        res = probe.probe_account(
            source="openai",
            account_alias="openai_main",
            mock_handler=mock_read_only_handler,
        )

        self.assertEqual(res.status, HEALTH_READY)
        self.assertFalse(res.purchases_made)
        self.assertFalse(res.provider_config_mutated)
        self.assertEqual(len(called_with), 1)
        self.assertEqual(called_with[0].account_alias, "openai_main")

    # -----------------------------------------------------------------------
    # US-02.b: BytePlus 5 Profiles & Process Isolation
    # -----------------------------------------------------------------------
    def test_us02_b_five_byteplus_profiles_addressed_explicitly_no_global_cli_mutation(self) -> None:
        """
        US-02.b: Fünf BytePlus-Profile werden explizit adressiert. Ein Test mit
        abwechselnden Profilen belegt, dass der aktive globale CLI-Account
        nicht still umgeschaltet wird.
        """
        profiles = ["bp01", "bp02", "bp03", "bp04", "bp05"]
        coordinator = BytePlusProfileCoordinator(
            profiles=profiles,
            state_root=self.state_root,
            cache_root=self.cache_root,
        )

        # Set a dummy global environment variable representing active global CLI profile
        initial_global_ark_profile = "default_user_account"
        os.environ["ARK_PROFILE"] = initial_global_ark_profile

        try:
            # Alternating execution across 5 profiles in alternating sequence
            execution_sequence = ["bp01", "bp03", "bp05", "bp02", "bp04", "bp01", "bp05"]

            for prof in execution_sequence:
                # Run an echo / inspection command
                res = coordinator.run_profile_command(
                    profile_id=prof,
                    command_args=["python3", "-c", "import os; print(os.environ.get('ARK_PROFILE'))"],
                )

                self.assertEqual(res["exit_code"], 0)
                # Subprocess sees isolated profile
                self.assertEqual(res["stdout"].strip(), prof)

                # Global process environment MUST remain untouched
                self.assertEqual(os.environ["ARK_PROFILE"], initial_global_ark_profile)

                # Profile binding metadata reflects explicit targeting and no global mutation
                binding = res["profile_binding"]
                self.assertEqual(binding["profile_id"], prof)
                self.assertTrue(binding["explicit"])
                self.assertFalse(binding["global_state_mutated"])

            # Verify that separate state & cache directories exist for each of the 5 profiles
            for prof in profiles:
                p_state = self.state_root / "byteplus" / prof
                p_cache = self.cache_root / "byteplus" / prof
                self.assertTrue(p_state.is_dir(), f"State directory missing for {prof}")
                self.assertTrue(p_cache.is_dir(), f"Cache directory missing for {prof}")

        finally:
            os.environ.pop("ARK_PROFILE", None)

    def test_us02_b_isolated_cache_and_state_paths_between_profiles(self) -> None:
        """Verify state files written in one profile do not leak into another."""
        coordinator = BytePlusProfileCoordinator(
            profiles=["bp01", "bp02"],
            state_root=self.state_root,
            cache_root=self.cache_root,
        )

        # Profile bp01 writes a marker file in its state dir
        res1 = coordinator.run_profile_command(
            profile_id="bp01",
            command_args=[
                "python3",
                "-c",
                "import os, pathlib; pathlib.Path(os.environ['XDG_DATA_HOME'], 'token_cache.json').write_text('token_for_bp01')",
            ],
        )
        self.assertEqual(res1["exit_code"], 0)

        # Profile bp02 checks its own XDG_DATA_HOME - it must NOT have bp01's file
        res2 = coordinator.run_profile_command(
            profile_id="bp02",
            command_args=[
                "python3",
                "-c",
                "import os, pathlib; print(pathlib.Path(os.environ['XDG_DATA_HOME'], 'token_cache.json').exists())",
            ],
        )
        self.assertEqual(res2["exit_code"], 0)
        self.assertEqual(res2["stdout"].strip(), "False")

        # bp01 does have it
        bp01_file = self.state_root / "byteplus" / "bp01" / "token_cache.json"
        self.assertTrue(bp01_file.exists())
        self.assertEqual(bp01_file.read_text(), "token_for_bp01")

        # bp02 does not have it
        bp02_file = self.state_root / "byteplus" / "bp02" / "token_cache.json"
        self.assertFalse(bp02_file.exists())

    # -----------------------------------------------------------------------
    # US-02.c: Secret Resolution, Missing Secret & Masking / Redaction Tests
    # -----------------------------------------------------------------------
    def test_us02_c_missing_secret_reference_produces_recoverable_error_without_secret_leak(self) -> None:
        """
        US-02.c: Ein fehlender Secretverweis erzeugt einen behebbaren
        Konfigurationsfehler ohne Ausgabe des Secrets.
        """
        secret_token = "sk-super-secret-production-token-987654"
        redactor = SecretRedactor([secret_token])
        resolver = SecretResolver(env={}, redactor=redactor)

        # Negative Probe 1: Non-existent env variable
        with self.assertRaises(MissingSecretRefError) as ctx:
            resolver.resolve(secret_ref="env:NON_EXISTENT_VAR_XYZ", account_alias="bp01")

        err = ctx.exception
        err_msg = str(err)

        # Error must be actionable/remediation present
        self.assertIn("NON_EXISTENT_VAR_XYZ", err_msg)
        self.assertIsNotNone(err.remediation)
        self.assertIn("Export environment variable", err.remediation)
        self.assertNotIn(secret_token, err_msg)

        # Negative Probe 2: Non-existent file secret
        with self.assertRaises(MissingSecretRefError) as ctx_file:
            resolver.resolve(secret_ref="file:/path/does/not/exist/secret.key", account_alias="bp02")

        self.assertIn("Create secret file", ctx_file.exception.remediation)

    def test_us02_c_secret_masking_in_exceptions_and_outputs(self) -> None:
        """Ensure secrets present in error messages or CLI output are automatically redacted."""
        real_api_key = "ark_sk_999888777666555444333222111"
        redactor = SecretRedactor([real_api_key])

        # Test direct text redaction
        leaked_message = f"Failed to connect using key {real_api_key} and Bearer abcdef123456789"
        sanitized = redactor.redact_text(leaked_message)

        self.assertNotIn(real_api_key, sanitized)
        self.assertNotIn("abcdef123456789", sanitized)
        self.assertIn(REDACTED_PLACEHOLDER, sanitized)

        # Test exception redaction
        err = AccountIsolationError(
            message=f"Authentication rejected for token {real_api_key}",
            account_alias="bp03",
            redactor=redactor,
        )
        self.assertNotIn(real_api_key, str(err))
        self.assertIn(REDACTED_PLACEHOLDER, str(err))

    def test_us02_c_file_based_secret_resolution(self) -> None:
        """Valid file-based secret resolution with restricted file reading."""
        secret_file = Path(self.temp_dir) / "bp_secret.txt"
        secret_file.write_text("my-confidential-token-123\n")

        resolver = SecretResolver(env={})
        resolved = resolver.resolve(f"file:{secret_file}", account_alias="bp03")
        self.assertEqual(resolved, "my-confidential-token-123")


if __name__ == "__main__":
    unittest.main()
