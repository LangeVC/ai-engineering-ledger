"""Artifact-scoped privacy scanning for AEL data exports.

The scanner deliberately reports rule identifiers, never matched values.  A
blocked artifact therefore cannot leak the canary that caused the decision and
does not prevent unrelated artifacts from being released.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


SCHEMA_VERSION = "privacy-scan-v0.3"
_ARTIFACT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_FORBIDDEN_FIELDS = {
    "chat",
    "code",
    "password",
    "prompt",
    "response",
    "screenshot",
    "secret",
    "token",
    "access_token",
    "api_token",
    "raw_evidence",
    "evidence_blob",
    "evidence_content",
}
_FORBIDDEN_SUFFIXES = (
    "_chat",
    "_code",
    "_password",
    "_prompt",
    "_response",
    "_secret",
    "_token",
    "_screenshot_path",
)
_SECRET = re.compile(
    r"(?i)(?:\bbearer(?:\s+|[-_:])\S{6,}|\b(?:sk[-_]|gh[opurs]_)[A-Za-z0-9_-]{6,}|"
    r"\b(?:api[_-]?key|token|secret|password)\s*[=:]\s*\S+)"
)
_EMAIL = re.compile(
    r"(?i)(?<![A-Z0-9._%+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![A-Z0-9._%+-])"
)
_PRIVATE_PATH = re.compile(
    r"(?:^|[\s\"'])(?:~[/\\]|/Users/|/home/|file:///(?:Users|home)/|[A-Za-z]:\\Users\\)"
)
_PRIVATE_REMOTE = re.compile(
    r"(?i)(?:\bgit@[^\s:]+:[^\s]+|\bssh://\S+|\bhttps?://[^\s]+\.git(?:\b|$)|"
    r"\bhttps?://[^\s/@:]+:[^\s/@]+@|\bhttps?://(?:localhost|127\.0\.0\.1|[^\s/]+\.(?:local|internal))\b)"
)


@dataclass(frozen=True)
class PrivacyProfile:
    """Selected public export surface and its approved stable aliases."""

    profile_id: str
    approved_aliases: frozenset[str]

    @classmethod
    def create(cls, profile_id: str, approved_aliases: Sequence[str]) -> "PrivacyProfile":
        if not _ARTIFACT_NAME.fullmatch(profile_id):
            raise ValueError("privacy profile id is invalid")
        aliases = frozenset(str(alias) for alias in approved_aliases)
        if any(not alias or len(alias) > 128 for alias in aliases):
            raise ValueError("privacy profile contains an invalid alias")
        return cls(profile_id=profile_id, approved_aliases=aliases)


class PrivacyViolation(ValueError):
    """Raised without embedding the matched private value."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _content_bytes(value: Any) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return _canonical(value)


def _text_rules(value: str) -> set[str]:
    rules: set[str] = set()
    if _SECRET.search(value):
        rules.add("SECRET_VALUE")
    if _EMAIL.search(value):
        rules.add("EMAIL_VALUE")
    if _PRIVATE_PATH.search(value):
        rules.add("PRIVATE_PATH")
    if _PRIVATE_REMOTE.search(value):
        rules.add("PRIVATE_REMOTE")
    return rules


def _scan(value: Any, profile: PrivacyProfile, rules: set[str], *, field: str | None = None) -> None:
    if field is not None:
        lowered = field.lower()
        if lowered in _FORBIDDEN_FIELDS or lowered.endswith(_FORBIDDEN_SUFFIXES):
            rules.add("FORBIDDEN_FIELD")
        if lowered in {"raw_evidence", "evidence_blob", "evidence_content"}:
            rules.add("RAW_EVIDENCE")
        rules.update(_text_rules(field))

    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            if lowered == "alias" or lowered.endswith("_alias"):
                if not isinstance(item, str) or item not in profile.approved_aliases:
                    rules.add("UNAPPROVED_ALIAS")
            _scan(item, profile, rules, field=key_text)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _scan(item, profile, rules)
    elif isinstance(value, str):
        rules.update(_text_rules(value))
    elif isinstance(value, (bytes, bytearray)):
        blob = bytes(value)
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError:
            rules.add("RAW_EVIDENCE")
        else:
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                rules.add("RAW_EVIDENCE")
            else:
                # Bytes are an evidence boundary.  Only an explicitly canonical
                # JSON representation is treated as structured export data;
                # arbitrary UTF-8 transcripts remain raw evidence.
                structured = isinstance(decoded, (Mapping, list))
                if not structured or blob not in {_canonical(decoded), _canonical(decoded) + b"\n"}:
                    rules.add("RAW_EVIDENCE")
                else:
                    _scan(decoded, profile, rules)


def scan_artifacts(
    artifacts: Mapping[str, Any], *, profile: PrivacyProfile
) -> dict[str, Any]:
    """Scan artifacts independently and return a redacted deterministic report."""
    results: list[dict[str, Any]] = []
    for artifact, payload in sorted(artifacts.items()):
        if _text_rules(artifact):
            raise ValueError("artifact name is sensitive")
        if not _ARTIFACT_NAME.fullmatch(artifact):
            raise ValueError("artifact name is invalid")
        rules: set[str] = set()
        _scan(payload, profile, rules)
        rule_ids = sorted(rules)
        results.append(
            {
                "artifact": artifact,
                "decision": "block" if rule_ids else "allow",
                "rule_ids": rule_ids,
                "content_sha256": hashlib.sha256(_content_bytes(payload)).hexdigest(),
            }
        )
    blocked = [item["artifact"] for item in results if item["decision"] == "block"]
    allowed = [item["artifact"] for item in results if item["decision"] == "allow"]
    return {
        "schema_version": SCHEMA_VERSION,
        "profile_id": profile.profile_id,
        "passed": not blocked,
        "allowed_artifacts": allowed,
        "blocked_artifacts": blocked,
        "artifacts": results,
    }


def approved_artifacts(
    artifacts: Mapping[str, Any], *, report: Mapping[str, Any], profile: PrivacyProfile
) -> dict[str, Any]:
    """Return allowed artifacts after re-scanning the exact payloads/profile."""
    verified_report = scan_artifacts(artifacts, profile=profile)
    if report != verified_report:
        raise ValueError("privacy report verification failed")
    decisions = {
        item["artifact"]: item
        for item in verified_report.get("artifacts", [])
        if isinstance(item, Mapping)
    }
    if set(decisions) != set(artifacts):
        raise ValueError("privacy report does not match the artifact set")
    for name, payload in artifacts.items():
        current_hash = hashlib.sha256(_content_bytes(payload)).hexdigest()
        if decisions[name].get("content_sha256") != current_hash:
            raise ValueError("artifact content changed after privacy scan")
    return {
        name: payload
        for name, payload in sorted(artifacts.items())
        if decisions[name].get("decision") == "allow"
    }


def require_artifact_allowed(
    report: Mapping[str, Any],
    artifact: str,
    *,
    payload: Any,
    profile: PrivacyProfile,
) -> None:
    """Re-scan and enforce one artifact without trusting caller decisions."""
    verified = scan_artifacts({artifact: payload}, profile=profile)
    expected = next(
        (
            item
            for item in report.get("artifacts", [])
            if isinstance(item, Mapping) and item.get("artifact") == artifact
        ),
        None,
    )
    actual = verified["artifacts"][0]
    if expected != actual or report.get("profile_id") != profile.profile_id:
        raise ValueError("privacy report verification failed")
    for item in verified["artifacts"]:
        if isinstance(item, Mapping) and item.get("artifact") == artifact:
            if item.get("decision") == "allow":
                return
            rules = ",".join(str(rule) for rule in item.get("rule_ids", []))
            raise PrivacyViolation(f"privacy scan blocked artifact {artifact}: {rules}")
    raise ValueError("artifact is absent from privacy report")


__all__ = [
    "PrivacyProfile",
    "PrivacyViolation",
    "approved_artifacts",
    "require_artifact_allowed",
    "scan_artifacts",
]
