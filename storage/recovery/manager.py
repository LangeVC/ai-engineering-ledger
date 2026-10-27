from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import secrets
import tempfile
from pathlib import Path
from typing import Any, Callable

SECRET = re.compile(r"(?i)(bearer\s+\S+|(?:sk|gh[opurs])_[A-Za-z0-9_-]+|(?:token|secret|password)\s*[=:]\s*\S+)")


def redact(value: Any) -> Any:
    if isinstance(value, str):
        value = SECRET.sub("[REDACTED]", value)
        if value.startswith("/") or re.match(r"^[A-Za-z]:\\", value):
            return "[REDACTED_PATH]"
        return value
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items() if k.lower() not in {"token", "secret", "password", "prompt", "code", "screenshot_path"}}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RecoveryManager:
    def secure_directory(self, path: str | os.PathLike[str]) -> list[str]:
        target = Path(path)
        warnings = []
        if target.exists() and target.stat().st_mode & 0o077:
            warnings.append(f"unsafe_permissions:{target}")
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(target, 0o700)
        return warnings

    def secure_file(self, path: str | os.PathLike[str], data: bytes) -> list[str]:
        target = Path(path)
        warnings = self.secure_directory(target.parent)
        if target.exists() and target.stat().st_mode & 0o077:
            warnings.append(f"unsafe_permissions:{target}")
        target.write_bytes(data)
        os.chmod(target, 0o600)
        return warnings

    def backup(self, destination: str | os.PathLike[str], database: str | os.PathLike[str],
               config: dict[str, Any], evidence_files: list[str | os.PathLike[str]]) -> dict[str, Any]:
        dest = Path(destination)
        if dest.exists() and any(dest.iterdir()):
            raise FileExistsError("backup destination must be empty")
        self.secure_directory(dest)
        files: dict[str, str] = {}
        db_out = dest / "ledger.db"
        shutil.copy2(database, db_out); os.chmod(db_out, 0o600); files["ledger.db"] = _hash(db_out)
        cfg = dest / "config.json"
        cfg.write_text(json.dumps(redact(config), sort_keys=True, indent=2) + "\n"); os.chmod(cfg, 0o600)
        files["config.json"] = _hash(cfg)
        ev_dir = dest / "evidence"; self.secure_directory(ev_dir)
        for src_name in evidence_files:
            src = Path(src_name)
            out = ev_dir / src.name
            shutil.copy2(src, out); os.chmod(out, 0o600); files[f"evidence/{src.name}"] = _hash(out)
        manifest = {"version": "0.1", "files": files}
        self.secure_file(dest / "manifest.json", (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode())
        return manifest

    def restore(self, backup: str | os.PathLike[str], destination: str | os.PathLike[str]) -> dict[str, Any]:
        src, dest = Path(backup), Path(destination)
        if dest.exists() and any(dest.iterdir()):
            raise FileExistsError("restore destination must be empty")
        manifest = json.loads((src / "manifest.json").read_text())
        for name, digest in manifest["files"].items():
            if _hash(src / name) != digest:
                raise ValueError(f"integrity failure: {name}")
        self.secure_directory(dest)
        for name in manifest["files"]:
            out = dest / name
            self.secure_directory(out.parent)
            shutil.copy2(src / name, out); os.chmod(out, 0o600)
        self.secure_file(dest / "manifest.json", (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode())
        return manifest

    def migrate_transactionally(self, database: str | os.PathLike[str], migration: Callable[[Path], None]) -> None:
        target = Path(database)
        with tempfile.TemporaryDirectory(dir=target.parent) as temp:
            candidate = Path(temp) / target.name
            shutil.copy2(target, candidate)
            migration(candidate)
            os.replace(candidate, target)

    def deletion_preview(self, records: list[dict[str, Any]], account: str) -> dict[str, Any]:
        affected = [r for r in records if r.get("account") == account]
        return {"account": account, "record_ids": [r.get("id") for r in affected],
                "evidence_ids": sorted({e for r in affected for e in r.get("evidence_ids", [])}),
                "derived_requires_rebuild": bool(affected), "exports_no_longer_reproducible": bool(affected),
                "external_cleanup": ["backups", "previous exports"]}

    def delete(self, records: list[dict[str, Any]], account: str, confirmed: bool) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if not confirmed:
            raise PermissionError("deletion requires explicit confirmation")
        remaining = [r for r in records if r.get("account") != account]
        return remaining, {"deletion_id": secrets.token_hex(16), "content_removed": True}
