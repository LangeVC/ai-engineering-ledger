"""Privacy-preserving local Claude Code usage adapter for AEL v0.3."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


PARSER_VERSION = "0.3.0"
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _load_checkpoint(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None or not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value.get("records", {}) if isinstance(value, dict) else {}


def _save_checkpoint(path: Path, records: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": "0.3", "parser_version": PARSER_VERSION, "records": records}
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(_canonical(payload) + b"\n")
    temporary.replace(path)


@dataclass(frozen=True)
class ClaudeAdapterConfig:
    account_alias: str = "claude-pro"
    checkpoint_path: Path | None = None


class ClaudeLocalAdapter:
    """Parse only ``message.usage`` metadata from injected Claude JSONL paths."""

    def __init__(self, config: ClaudeAdapterConfig | None = None) -> None:
        self.config = config or ClaudeAdapterConfig()

    @staticmethod
    def discover(roots: Iterable[Path]) -> list[Path]:
        files: set[Path] = set()
        for root in roots:
            candidate = Path(root)
            if candidate.is_file() and candidate.suffix == ".jsonl":
                files.add(candidate)
            elif candidate.is_dir():
                files.update(path for path in candidate.rglob("*.jsonl") if path.is_file())
        return sorted(files, key=lambda item: str(item))

    def collect(self, roots: Iterable[Path], *, dry_run: bool = False) -> dict[str, Any]:
        files = self.discover(roots)
        checkpoint = _load_checkpoint(self.config.checkpoint_path)
        next_checkpoint = dict(checkpoint)
        records: list[dict[str, Any]] = []
        file_manifest: list[dict[str, Any]] = []
        quarantined: list[dict[str, Any]] = []
        observed: list[str] = []
        unchanged = 0

        for path in files:
            content = path.read_bytes()
            accepted_in_file = 0
            for line_number, raw_line in enumerate(content.splitlines(), 1):
                if not raw_line.strip():
                    continue
                try:
                    envelope = json.loads(raw_line)
                    record = self._parse_envelope(envelope)
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    quarantined.append(
                        {"file_sha256": _digest(content), "line": line_number, "reason": str(exc)}
                    )
                    continue
                if record is None:
                    continue

                native_key = f"{record['session_id'] or 'unknown'}:{record['request_id']}"
                content_hash = _digest(_canonical(record))
                # Compare against the evolving state, not only the state loaded
                # at run start. This makes duplicate native IDs deterministic
                # within one JSONL scan as well as across later scans.
                previous = next_checkpoint.get(native_key)
                versions = dict(previous.get("versions", {})) if previous else {}
                if previous and previous.get("content_hash") not in versions:
                    # Upgrade checkpoints written before intra-run history was
                    # tracked without invalidating their current revision.
                    versions[previous["content_hash"]] = {
                        "revision": int(previous["revision"]),
                        "record_id": previous["record_id"],
                    }
                if previous and content_hash in versions:
                    unchanged += 1
                    continue

                revision = 1 if previous is None else int(previous["revision"]) + 1
                record_id = _digest(f"claude:{native_key}:{revision}:{content_hash}".encode())
                record["revision"] = {
                    "number": revision,
                    "supersedes": None if previous is None else previous["record_id"],
                    "comparison": "new" if previous is None else "content_changed",
                }
                record["record_id"] = record_id
                record["content_hash"] = content_hash
                records.append(record)
                accepted_in_file += 1
                if record["observed_at"]:
                    observed.append(record["observed_at"])
                next_checkpoint[native_key] = {
                    "content_hash": content_hash,
                    "revision": revision,
                    "record_id": record_id,
                    "versions": {
                        **versions,
                        content_hash: {"revision": revision, "record_id": record_id},
                    },
                }
            file_manifest.append(
                {
                    "file_sha256": _digest(content),
                    "size_bytes": len(content),
                    "accepted_records": accepted_in_file,
                }
            )

        if not dry_run and self.config.checkpoint_path is not None:
            _save_checkpoint(self.config.checkpoint_path, next_checkpoint)

        records.sort(key=lambda item: (item["observed_at"] or "", item["request_id"]))
        return {
            "source": "claude",
            "account_alias": self.config.account_alias,
            "parser_version": PARSER_VERSION,
            "dry_run": dry_run,
            "records": records,
            "manifest": {
                "files": file_manifest,
                "files_discovered": len(files),
                "records_emitted": len(records),
                "unchanged_records": unchanged,
                "quarantined": quarantined,
                "observed_from": min(observed) if observed else None,
                "observed_to": max(observed) if observed else None,
                "coverage": "partial" if files else "unavailable",
                "limitations": [
                    "Local Claude transcript files prove observed local activity, not complete provider history or subscription billing."
                ],
            },
            "checkpoint": next_checkpoint,
        }

    def _parse_envelope(self, envelope: Any) -> dict[str, Any] | None:
        if not isinstance(envelope, dict):
            raise TypeError("JSONL line must be an object")
        message = envelope.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
            return None
        usage = message["usage"]
        request_id = message.get("id") or envelope.get("requestId") or envelope.get("uuid")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("usage record has no stable request ID")

        tokens: dict[str, int] = {}
        for field in TOKEN_FIELDS:
            raw = usage.get(field, 0)
            if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
                raise ValueError(f"{field} must be a non-negative integer")
            tokens[field] = raw

        return {
            "source": "claude",
            "account_alias": self.config.account_alias,
            "auth_mode": "subscription",
            "plan": "pro",
            "semantic_family": "raw_token_activity",
            "billing_assertion": False,
            "request_id": request_id,
            "session_id": envelope.get("sessionId") or envelope.get("session_id"),
            "model": message.get("model"),
            "observed_at": envelope.get("timestamp") or message.get("timestamp"),
            "input_tokens": tokens["input_tokens"],
            "output_tokens": tokens["output_tokens"],
            "cache_read_tokens": tokens["cache_read_input_tokens"],
            "cache_creation_tokens": tokens["cache_creation_input_tokens"],
        }
