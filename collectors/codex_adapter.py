"""Privacy-preserving local Codex session usage adapter for AEL v0.3."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


PARSER_VERSION = "0.3.0"
TOKEN_ALIASES = {
    "input_tokens": ("input_tokens",),
    "output_tokens": ("output_tokens",),
    "reasoning_tokens": ("reasoning_tokens",),
    "cached_input_tokens": ("cached_input_tokens", "cache_read_input_tokens"),
}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _checkpoint(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None or not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value.get("records", {}) if isinstance(value, dict) else {}


def _save(path: Path, records: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(
        _canonical({"schema_version": "0.3", "parser_version": PARSER_VERSION, "records": records}) + b"\n"
    )
    temporary.replace(path)


@dataclass(frozen=True)
class CodexAdapterConfig:
    account_alias: str = "codex-plus"
    auth_mode: str = "subscription"
    plan: str = "plus"
    checkpoint_path: Path | None = None

    def __post_init__(self) -> None:
        if self.auth_mode not in {"subscription", "api_key", "oauth", "local_session"}:
            raise ValueError("unsupported auth_mode")
        if self.auth_mode == "api_key" and self.plan == "plus":
            raise ValueError("Plus and API-key usage require separate source identities")


class CodexLocalAdapter:
    """Parse supported Codex JSON/JSONL usage records from explicitly injected roots."""

    def __init__(self, config: CodexAdapterConfig | None = None) -> None:
        self.config = config or CodexAdapterConfig()

    @staticmethod
    def discover(roots: Iterable[Path]) -> list[Path]:
        files: set[Path] = set()
        for root in roots:
            candidate = Path(root)
            if candidate.is_file() and candidate.suffix in {".json", ".jsonl"}:
                files.add(candidate)
            elif candidate.is_dir():
                files.update(
                    path for path in candidate.rglob("*") if path.is_file() and path.suffix in {".json", ".jsonl"}
                )
        return sorted(files, key=lambda item: str(item))

    def collect(self, roots: Iterable[Path], *, dry_run: bool = False) -> dict[str, Any]:
        files = self.discover(roots)
        previous = _checkpoint(self.config.checkpoint_path)
        current = dict(previous)
        records: list[dict[str, Any]] = []
        manifest_files: list[dict[str, Any]] = []
        quarantined: list[dict[str, Any]] = []
        unsupported_shapes = 0
        unchanged = 0
        observed: list[str] = []

        for path in files:
            content = path.read_bytes()
            values = self._decode_file(content, path.suffix, quarantined)
            # Keep private local paths out of emitted records while still giving
            # ID-less records a stable coordinate that cannot collide across
            # files.  The ordinal is local to that file, not the discovery list.
            source_coordinate = _digest(str(path.resolve()).encode("utf-8"))
            accepted = 0
            for ordinal, value in enumerate(values, 1):
                try:
                    record = self._parse(value, ordinal, source_coordinate)
                except (TypeError, ValueError) as exc:
                    quarantined.append(
                        {"file_sha256": _digest(content), "record": ordinal, "reason": str(exc)}
                    )
                    continue
                if record is None:
                    unsupported_shapes += 1
                    continue
                key = f"{record['session_id'] or 'unknown'}:{record['turn_id'] or 'unknown'}:{record['request_id']}"
                content_hash = _digest(_canonical(record))
                # Compare against the evolving state, not only the checkpoint
                # loaded at the start.  A single file can contain repeated or
                # revised events for the same native identity.
                old = current.get(key)
                versions = dict(old.get("versions", {})) if old else {}
                if old and old.get("content_hash") not in versions:
                    # Backward-compatible upgrade for checkpoints written
                    # before per-native-ID version history was recorded.
                    versions[old["content_hash"]] = {
                        "revision": int(old["revision"]),
                        "record_id": old["record_id"],
                    }
                if old and content_hash in versions:
                    unchanged += 1
                    continue
                revision = 1 if old is None else int(old["revision"]) + 1
                record_id = _digest(f"codex:{key}:{revision}:{content_hash}".encode())
                record["revision"] = {
                    "number": revision,
                    "supersedes": None if old is None else old["record_id"],
                    "comparison": "new" if old is None else "content_changed",
                }
                record["record_id"] = record_id
                record["content_hash"] = content_hash
                current[key] = {
                    "content_hash": content_hash,
                    "revision": revision,
                    "record_id": record_id,
                    "versions": {
                        **versions,
                        content_hash: {"revision": revision, "record_id": record_id},
                    },
                }
                records.append(record)
                accepted += 1
                if record["observed_at"]:
                    observed.append(record["observed_at"])
            manifest_files.append(
                {"file_sha256": _digest(content), "size_bytes": len(content), "accepted_records": accepted}
            )

        if not dry_run and self.config.checkpoint_path is not None:
            _save(self.config.checkpoint_path, current)

        records.sort(key=lambda item: (item["observed_at"] or "", item["request_id"]))
        if records or unchanged:
            capability = "supported"
            coverage = "partial"
            reason = "Supported local Codex usage records were observed."
        elif files:
            capability = "unsupported_shape"
            coverage = "unavailable"
            reason = "Files were discovered but no supported usage shape was found."
        else:
            capability = "unavailable"
            coverage = "unavailable"
            reason = "No injected Codex session or usage files were discovered."

        return {
            "source": "codex",
            "account_alias": self.config.account_alias,
            "auth_mode": self.config.auth_mode,
            "plan": self.config.plan,
            "parser_version": PARSER_VERSION,
            "dry_run": dry_run,
            "capability": {"status": capability, "reason": reason},
            "records": records,
            "manifest": {
                "files": manifest_files,
                "files_discovered": len(files),
                "records_emitted": len(records),
                "unchanged_records": unchanged,
                "unsupported_shapes": unsupported_shapes,
                "quarantined": quarantined,
                "observed_from": min(observed) if observed else None,
                "observed_to": max(observed) if observed else None,
                "coverage": coverage,
                "limitations": [
                    "Local Codex records prove observed activity only; token fields are not Plus credits, allowance, invoice, or billing truth."
                ],
            },
            "checkpoint": current,
        }

    @staticmethod
    def _decode_file(content: bytes, suffix: str, quarantined: list[dict[str, Any]]) -> list[Any]:
        if suffix == ".json":
            try:
                value = json.loads(content)
            except json.JSONDecodeError as exc:
                quarantined.append({"file_sha256": _digest(content), "reason": str(exc)})
                return []
            if isinstance(value, list):
                return value
            return [value]
        values: list[Any] = []
        for line, raw in enumerate(content.splitlines(), 1):
            if not raw.strip():
                continue
            try:
                values.append(json.loads(raw))
            except json.JSONDecodeError as exc:
                quarantined.append({"file_sha256": _digest(content), "line": line, "reason": str(exc)})
        return values

    def _parse(
        self,
        envelope: Any,
        ordinal: int,
        source_coordinate: str,
    ) -> dict[str, Any] | None:
        if not isinstance(envelope, dict):
            raise TypeError("usage record must be an object")
        payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}

        usage = envelope.get("usage")
        scope_level = "request"
        if not isinstance(usage, dict):
            usage = payload.get("usage")
        if not isinstance(usage, dict):
            usage = info.get("last_token_usage")
        if not isinstance(usage, dict):
            usage = info.get("total_token_usage")
            scope_level = "session_total"
        if not isinstance(usage, dict):
            return None

        tokens: dict[str, int] = {}
        for normalized, aliases in TOKEN_ALIASES.items():
            raw: Any = 0
            for alias in aliases:
                if alias in usage:
                    raw = usage[alias]
                    break
            if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
                raise ValueError(f"{normalized} must be a non-negative integer")
            tokens[normalized] = raw

        request_id = (
            envelope.get("request_id")
            or payload.get("request_id")
            or payload.get("id")
            or envelope.get("id")
            or f"anonymous-{source_coordinate[:24]}-{ordinal}"
        )
        return {
            "source": "codex",
            "account_alias": self.config.account_alias,
            "auth_mode": self.config.auth_mode,
            "plan": self.config.plan,
            "semantic_family": "raw_token_activity",
            "billing_assertion": False,
            "scope_level": scope_level,
            "request_id": str(request_id),
            "session_id": envelope.get("session_id") or payload.get("session_id"),
            "turn_id": envelope.get("turn_id") or payload.get("turn_id"),
            "model": envelope.get("model") or payload.get("model") or info.get("model"),
            "observed_at": envelope.get("timestamp") or payload.get("timestamp"),
            **tokens,
        }
