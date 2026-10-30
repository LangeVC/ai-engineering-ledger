"""Dependency-free, loopback-only browser UI for AI Engineering Ledger."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import shutil
import sqlite3
import struct
import subprocess
import zlib
import threading
import time
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ael.ledger import OperationalLedger, normalize_capture
from ael.paths import resolve_database, setup_workspace
from importers.manual.importer import export_canonical_measurements, parse_manual_template_json


SCHEMA_VERSION = "0.1"
MAX_BODY = 14 * 1024 * 1024
MAX_UPLOAD = 10 * 1024 * 1024
SESSION_COOKIE = "ael_session"
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "X-Frame-Options": "DENY",
}
IMAGE_MAGIC = {
    "image/png": lambda raw: raw.startswith(b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": lambda raw: raw.startswith(b"\xff\xd8\xff"),
    "image/webp": lambda raw: len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP",
}


def validate_image(raw: bytes, media_type: str) -> tuple[int, int]:
    """Validate container structure and return bounded dimensions."""
    check = IMAGE_MAGIC.get(media_type)
    if check is None or not check(raw):
        raise ValueError("image media type and bytes do not match")
    width = height = 0
    if media_type == "image/png":
        if len(raw) < 45 or raw[8:12] != b"\x00\x00\x00\r" or raw[12:16] != b"IHDR" or b"IEND" not in raw[-24:]:
            raise ValueError("malformed PNG image")
        width, height = struct.unpack(">II", raw[16:24])
    elif media_type == "image/jpeg":
        if not raw.endswith(b"\xff\xd9"):
            raise ValueError("malformed JPEG image")
        index = 2
        while index + 9 < len(raw):
            if raw[index] != 0xFF:
                index += 1
                continue
            marker = raw[index + 1]
            index += 2
            if marker in {0xD8, 0xD9}:
                continue
            if index + 2 > len(raw):
                break
            length = int.from_bytes(raw[index:index + 2], "big")
            if length < 2 or index + length > len(raw):
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                height = int.from_bytes(raw[index + 3:index + 5], "big")
                width = int.from_bytes(raw[index + 5:index + 7], "big")
                break
            index += length
    else:
        if int.from_bytes(raw[4:8], "little") + 8 != len(raw):
            raise ValueError("malformed WebP image")
        chunk = raw[12:16]
        if chunk == b"VP8X" and len(raw) >= 30:
            width = 1 + int.from_bytes(raw[24:27], "little")
            height = 1 + int.from_bytes(raw[27:30], "little")
        elif chunk == b"VP8 " and len(raw) >= 30 and raw[23:26] == b"\x9d\x01\x2a":
            width = int.from_bytes(raw[26:28], "little") & 0x3FFF
            height = int.from_bytes(raw[28:30], "little") & 0x3FFF
        elif chunk == b"VP8L" and len(raw) >= 25 and raw[20] == 0x2F:
            bits = int.from_bytes(raw[21:25], "little")
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
    if width <= 0 or height <= 0 or width > 10_000 or height > 10_000 or width * height > 40_000_000:
        raise ValueError("image dimensions are invalid or exceed safety limits")
    return width, height


def normalize_png_for_ocr(raw: bytes) -> tuple[bytes, int, int]:
    """Strictly decode an 8-bit, non-interlaced PNG into a normalized PGM."""
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("malformed PNG image")
    position = 8
    ihdr: bytes | None = None
    compressed = bytearray()
    saw_iend = False
    while position + 12 <= len(raw):
        length = int.from_bytes(raw[position:position + 4], "big")
        chunk_type = raw[position + 4:position + 8]
        end = position + 12 + length
        if length > MAX_UPLOAD or end > len(raw):
            raise ValueError("malformed PNG image")
        payload = raw[position + 8:position + 8 + length]
        expected_crc = int.from_bytes(raw[position + 8 + length:end], "big")
        if zlib.crc32(chunk_type + payload) & 0xFFFFFFFF != expected_crc:
            raise ValueError("malformed PNG image")
        if chunk_type == b"IHDR":
            if ihdr is not None or length != 13:
                raise ValueError("malformed PNG image")
            ihdr = payload
        elif chunk_type == b"IDAT":
            compressed.extend(payload)
            if len(compressed) > MAX_UPLOAD:
                raise ValueError("compressed PNG data exceeds safety limit")
        elif chunk_type == b"IEND":
            if length != 0 or end != len(raw):
                raise ValueError("malformed PNG image")
            saw_iend = True
            break
        position = end
    if ihdr is None or not saw_iend:
        raise ValueError("malformed PNG image")
    width, height, depth, color_type, compression, filter_method, interlace = struct.unpack(">IIBBBBB", ihdr)
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color_type)
    if (depth, compression, filter_method, interlace) != (8, 0, 0, 0) or channels is None:
        raise ValueError("PNG format is unsupported for safe local OCR")
    if width <= 0 or height <= 0 or width > 10_000 or height > 10_000 or width * height > 20_000_000:
        raise ValueError("image dimensions are invalid or exceed safety limits")
    row_bytes = width * channels
    expected = (row_bytes + 1) * height
    decompressor = zlib.decompressobj()
    decoded = decompressor.decompress(bytes(compressed), expected + 1)
    decoded += decompressor.flush()
    if len(decoded) != expected or not decompressor.eof or decompressor.unused_data or decompressor.unconsumed_tail:
        raise ValueError("PNG decoded size is invalid")
    rows: list[bytearray] = []
    offset = 0
    for _ in range(height):
        filter_type = decoded[offset]
        scan = bytearray(decoded[offset + 1:offset + 1 + row_bytes])
        offset += row_bytes + 1
        previous = rows[-1] if rows else bytearray(row_bytes)
        for index in range(row_bytes):
            left = scan[index - channels] if index >= channels else 0
            above = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 1:
                scan[index] = (scan[index] + left) & 0xFF
            elif filter_type == 2:
                scan[index] = (scan[index] + above) & 0xFF
            elif filter_type == 3:
                scan[index] = (scan[index] + ((left + above) // 2)) & 0xFF
            elif filter_type == 4:
                predictor = left + above - upper_left
                pa, pb, pc = abs(predictor - left), abs(predictor - above), abs(predictor - upper_left)
                scan[index] = (scan[index] + (left if pa <= pb and pa <= pc else above if pb <= pc else upper_left)) & 0xFF
            elif filter_type != 0:
                raise ValueError("PNG uses an invalid row filter")
        rows.append(scan)
    grayscale = bytearray(width * height)
    target = 0
    for row in rows:
        for index in range(0, len(row), channels):
            if color_type in {0, 4}:
                gray = row[index]
            else:
                gray = (77 * row[index] + 150 * row[index + 1] + 29 * row[index + 2]) >> 8
            grayscale[target] = gray
            target += 1
    return f"P5\n{width} {height}\n255\n".encode("ascii") + bytes(grayscale), width, height


def extract_screenshot(path: Path, source: str) -> dict[str, Any]:
    """Run optional local Tesseract and return evidence, never semantic guesses."""
    executable = shutil.which("tesseract")
    if not executable:
        return {
            "extractor": {"id": "manual-review", "version": "0.1", "available": False},
            "status": "manual_review_required",
            "fields": [],
            "text": None,
        }
    try:
        result = subprocess.run(
            [executable, str(path), "stdout", "--psm", "6"],
            check=False, capture_output=True, text=True, timeout=20,
            env={"PATH": os.environ.get("PATH", "")},
        )
    except (OSError, subprocess.TimeoutExpired):
        return {
            "extractor": {"id": "tesseract", "version": "local", "available": True},
            "status": "manual_review_required",
            "fields": [],
            "text": None,
        }
    text = result.stdout[:20_000] if result.returncode == 0 else ""
    fields: list[dict[str, Any]] = []
    # Numeric OCR candidates are deliberately unclassified. The human assigns
    # provider semantics in the guided form before confirmation.
    import re
    for line_number, line in enumerate(text.splitlines(), start=1):
        for match in re.finditer(r"(?<![\d.])(\d{1,3}(?:[.,]\d+)?)\s*%", line):
            value = match.group(1).replace(",", ".")
            fields.append({"name": "unclassified_percent", "value": value,
                           "unit": "percent", "confidence": "0.5",
                           "source_region": {"line": line_number}, "unknown": False})
        for match in re.finditer(r"(?i)(\d+(?:[.,]\d+)?)\s*(credits?)\b", line):
            value = match.group(1).replace(",", ".")
            fields.append({"name": "unclassified_credit", "value": value,
                           "unit": "credit", "confidence": "0.5",
                           "source_region": {"line": line_number}, "unknown": False})
    return {
        "extractor": {"id": "tesseract", "version": "local", "available": True},
        "status": "manual_review_required",
        "fields": fields,
        "text": text or None,
        "provider_template": source,
    }


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _safe_name(value: str) -> str:
    return Path(value).name[:120] or "capture"


def _write_private(path: Path, raw: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, raw)
    finally:
        os.close(descriptor)


def _is_loopback(bind: str) -> bool:
    return bind in {"127.0.0.1", "::1"}


class AliasStore:
    """Versioned, secret-free alias configuration."""

    def __init__(self, home: Path):
        self.path = home / "gui-config.json"
        self._lock = threading.RLock()

    def load(self) -> dict[str, Any]:
        with self._lock:
            if self.path.is_symlink():
                raise ValueError("refusing symlinked GUI configuration")
            if not self.path.exists():
                return {"schema_version": SCHEMA_VERSION, "aliases": []}
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data.get("aliases"), list):
                raise ValueError("invalid alias configuration")
            return data

    def save(self, data: dict[str, Any]) -> None:
        with self._lock:
            temp = self.path.with_name("." + self.path.name + "." + secrets.token_hex(8))
            descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(descriptor, _json_bytes(data) + b"\n")
            finally:
                os.close(descriptor)
            os.replace(temp, self.path)

    @staticmethod
    def validate(alias: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(alias, dict):
            raise ValueError("alias must be an object")
        allowed = {"id", "provider", "capabilities", "cadence", "secret_ref", "label"}
        forbidden_markers = ("password", "api_key", "apikey", "access_token", "credential")
        unknown = set(alias) - allowed
        if unknown or any(any(marker in key.lower() for marker in forbidden_markers) for key in alias):
            raise ValueError("credential or unsupported alias fields are not allowed")
        alias_id = str(alias.get("id", "")).strip()
        provider = str(alias.get("provider", "")).strip()
        if not alias_id or not provider or len(alias_id) > 80:
            raise ValueError("alias id and provider are required")
        if any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for char in alias_id):
            raise ValueError("alias id contains unsupported characters")
        secret_ref = alias.get("secret_ref")
        if secret_ref is not None and not str(secret_ref).startswith(("keychain:", "env:")):
            raise ValueError("secret_ref must be an opaque keychain: or env: reference")
        return {
            "id": alias_id,
            "provider": provider,
            "label": str(alias.get("label", alias_id))[:120],
            "capabilities": list(alias.get("capabilities", [])),
            "cadence": str(alias.get("cadence", "manual"))[:40],
            "secret_ref": secret_ref,
        }

    def upsert(self, alias: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            clean = self.validate(alias)
            data = self.load()
            aliases = [item for item in data["aliases"] if item.get("id") != clean["id"]]
            aliases.append(clean)
            data["aliases"] = sorted(aliases, key=lambda item: item["id"])
            self.save(data)
            return clean

    def delete(self, alias_id: str, confirmation: str) -> bool:
        with self._lock:
            if not hmac.compare_digest(alias_id, confirmation):
                raise ValueError("confirmation must exactly match alias id")
            data = self.load()
            before = len(data["aliases"])
            data["aliases"] = [item for item in data["aliases"] if item.get("id") != alias_id]
            self.save(data)
            return len(data["aliases"]) != before


@dataclass
class Draft:
    draft_id: str
    kind: str
    source: str
    account: str
    import_kind: str
    path: Path
    evidence_sha256: str
    created_at: float
    payload: dict[str, Any]
    observed_at: str | None = None


class DraftStore:
    def __init__(self, home: Path, ttl_seconds: int = 24 * 60 * 60):
        self.root = home / "inbox" / "gui-staging"
        if self.root.is_symlink():
            raise ValueError("refusing symlinked GUI staging directory")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.ttl_seconds = ttl_seconds
        self._drafts: dict[str, Draft] = {}
        self._lock = threading.RLock()
        self.cleanup()

    def cleanup(self) -> None:
        cutoff = time.time() - self.ttl_seconds
        for path in self.root.iterdir():
            if path.is_symlink():
                continue
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()

    def create(self, *, kind: str, source: str, account: str, import_kind: str,
               raw: bytes, suffix: str, payload: dict[str, Any],
               observed_at: str | None = None) -> Draft:
        draft_id = "draft_" + secrets.token_hex(12)
        path = self.root / (secrets.token_hex(16) + suffix)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, raw)
        finally:
            os.close(descriptor)
        evidence_sha = hashlib.sha256(raw).hexdigest()
        draft = Draft(draft_id, kind, source, account, import_kind, path,
                      evidence_sha, time.time(), payload, observed_at)
        with self._lock:
            self._drafts[draft_id] = draft
        return draft

    def get(self, draft_id: str, evidence_sha256: str) -> Draft:
        with self._lock:
            draft = self._drafts.get(draft_id)
            if draft is None or time.time() - draft.created_at > self.ttl_seconds:
                raise ValueError("unknown, stale or modified draft")
            if not hmac.compare_digest(draft.evidence_sha256, evidence_sha256):
                raise ValueError("unknown, stale or modified draft")
            actual_sha = hashlib.sha256(draft.path.read_bytes()).hexdigest()
            if not hmac.compare_digest(draft.evidence_sha256, actual_sha):
                raise ValueError("unknown, stale or modified draft")
            return draft

    def claim(self, draft_id: str, evidence_sha256: str) -> Draft:
        with self._lock:
            draft = self.get(draft_id, evidence_sha256)
            self._drafts.pop(draft_id, None)
            return draft

    def consume(self, draft: Draft) -> None:
        with self._lock:
            self._drafts.pop(draft.draft_id, None)
            if draft.path.exists() and not draft.path.is_symlink():
                draft.path.unlink()

    def discard(self, draft_id: str, evidence_sha256: str) -> None:
        self.consume(self.claim(draft_id, evidence_sha256))


def preview_capture(database: Path, *, source: str, account: str, kind: str,
                    raw: bytes, observed_at: str | None) -> dict[str, Any]:
    """Normalize and count without inserting observations or receipts."""
    evidence_sha = hashlib.sha256(raw).hexdigest()
    observations, rejected, errors, incomplete = normalize_capture(
        source=source, account=account, kind=kind, raw=raw,
        evidence_sha256=evidence_sha, observed_at=observed_at,
    )
    keys = [item["idempotency_key"] for item in observations]
    unique_keys = list(dict.fromkeys(keys))
    existing_keys: set[str] = set()
    if unique_keys and database.exists():
        with sqlite3.connect(database) as connection:
            for offset in range(0, len(unique_keys), 500):
                chunk = unique_keys[offset:offset + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"SELECT idempotency_key FROM observations WHERE idempotency_key IN ({placeholders})", chunk
                ).fetchall()
                existing_keys.update(row[0] for row in rows)
    accepted = len(unique_keys) - len(existing_keys)
    existing = len(observations) - accepted
    return {
        "schema_version": SCHEMA_VERSION,
        "evidence_sha256": evidence_sha,
        "observations": observations,
        "accepted": accepted,
        "existing": existing,
        "rejected": rejected,
        "incomplete": bool(incomplete),
        "errors": errors,
    }


class AELWebServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], home: Path, database: Path):
        super().__init__(address, AELRequestHandler)
        self.home = home
        self.database = database
        self.aliases = AliasStore(home)
        self.drafts = DraftStore(home)
        self.bootstrap_token = secrets.token_urlsafe(32)
        self.bootstrap_used = False
        self.session_token: str | None = None
        self.csrf_token: str | None = None
        self.state_lock = threading.RLock()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"

    @property
    def launch_url(self) -> str:
        return self.origin + "/#bootstrap=" + self.bootstrap_token


class AELRequestHandler(BaseHTTPRequestHandler):
    server: AELWebServer

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def end_headers(self) -> None:
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        super().end_headers()

    def _host_ok(self) -> bool:
        return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

    def _origin_ok(self) -> bool:
        return self.headers.get("Origin") == self.server.origin

    def _session_ok(self) -> bool:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        value = cookie.get(SESSION_COOKIE)
        return bool(value and self.server.session_token and
                    hmac.compare_digest(value.value, self.server.session_token))

    def _csrf_ok(self) -> bool:
        supplied = self.headers.get("X-AEL-CSRF", "")
        return bool(self.server.csrf_token and hmac.compare_digest(supplied, self.server.csrf_token))

    def _write(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, *, data: object | None = None,
              error: str | None = None) -> None:
        payload: dict[str, object] = {"schema_version": SCHEMA_VERSION,
                                     "status": "success" if status < 400 else "failure"}
        if error is not None:
            payload["error"] = error
        else:
            payload["data"] = data
        self._write(status, _json_bytes(payload), "application/json; charset=utf-8")

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid content length") from exc
        if length <= 0 or length > MAX_BODY:
            raise ValueError("request body size is invalid")
        raw = self.rfile.read(length)
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("JSON object required")
        return data

    def _decode_upload(self, data: dict[str, Any]) -> bytes:
        try:
            raw = base64.b64decode(str(data["content_base64"]), validate=True)
        except Exception as exc:
            raise ValueError("invalid base64 upload") from exc
        if len(raw) > MAX_UPLOAD:
            raise ValueError("upload exceeds 10 MiB")
        return raw

    def _require_mutation_auth(self) -> bool:
        if not self._origin_ok() or not self._session_ok() or not self._csrf_ok():
            self._json(HTTPStatus.FORBIDDEN, error="mutation authorization failed")
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._json(HTTPStatus.MISDIRECTED_REQUEST, error="invalid host")
            return
        request = urlparse(self.path)
        path = request.path
        if path in {"/", "/index.html"}:
            self._asset("index.html", "text/html; charset=utf-8")
            return
        if path == "/app.css":
            self._asset("app.css", "text/css; charset=utf-8")
            return
        if path == "/app.js":
            self._asset("app.js", "text/javascript; charset=utf-8")
            return
        if not self._session_ok():
            self._json(HTTPStatus.UNAUTHORIZED, error="session required")
            return
        if path == "/api/health":
            self._json(HTTPStatus.OK, data={"ready": True, "origin": self.server.origin})
        elif path == "/api/session":
            self._json(HTTPStatus.OK, data={"csrf_token": self.server.csrf_token})
        elif path == "/api/config":
            self._json(HTTPStatus.OK, data=self.server.aliases.load())
        elif path == "/api/reports/providers":
            self._json(HTTPStatus.OK, data=OperationalLedger(self.server.database).provider_report())
        elif path == "/api/reports/study":
            query = parse_qs(request.query, keep_blank_values=False)
            unexpected = set(query) - {"start", "end_exclusive"}
            if unexpected or any(len(values) != 1 for values in query.values()):
                self._json(HTTPStatus.BAD_REQUEST, error="invalid study filter")
                return
            try:
                report = OperationalLedger(self.server.database).study_report(
                    start=query.get("start", [None])[0],
                    end_exclusive=query.get("end_exclusive", [None])[0],
                )
            except ValueError:
                self._json(HTTPStatus.BAD_REQUEST, error="invalid study filter")
                return
            self._json(HTTPStatus.OK, data=report)
        else:
            self._json(HTTPStatus.NOT_FOUND, error="not found")

    def _asset(self, name: str, content_type: str) -> None:
        body = files("ael.assets").joinpath(name).read_bytes()
        self._write(HTTPStatus.OK, body, content_type)

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._json(HTTPStatus.MISDIRECTED_REQUEST, error="invalid host")
            return
        path = urlparse(self.path).path
        try:
            if path == "/api/session/bootstrap":
                self._bootstrap()
                return
            if not self._require_mutation_auth():
                return
            data = self._read_json()
            if path == "/api/config/aliases":
                self._json(HTTPStatus.OK, data=self.server.aliases.upsert(data.get("alias", {})))
            elif path == "/api/import/preview":
                self._import_preview(data)
            elif path == "/api/import/confirm":
                self._import_confirm(data)
            elif path == "/api/drafts/discard":
                self.server.drafts.discard(str(data.get("draft_id", "")), str(data.get("evidence_sha256", "")))
                self._json(HTTPStatus.OK, data={"discarded": True})
            elif path == "/api/screenshots/draft":
                self._screenshot_draft(data)
            elif path == "/api/screenshots/confirm":
                self._screenshot_confirm(data)
            elif path == "/api/config/aliases/delete":
                deleted = self.server.aliases.delete(str(data.get("id", "")), str(data.get("confirm", "")))
                self._json(HTTPStatus.OK, data={"deleted": deleted, "ledger_observations_retained": True})
            else:
                self._json(HTTPStatus.NOT_FOUND, error="not found")
        except (ValueError, TypeError, AttributeError, json.JSONDecodeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, error=str(exc)[:240])
        except (OSError, sqlite3.Error):
            self._json(HTTPStatus.BAD_REQUEST, error="local operation failed")

    def _bootstrap(self) -> None:
        with self.server.state_lock:
            if not self._origin_ok() or self.server.bootstrap_used:
                self._json(HTTPStatus.FORBIDDEN, error="bootstrap unavailable")
                return
            data = self._read_json()
            supplied = str(data.get("token", ""))
            if not hmac.compare_digest(supplied, self.server.bootstrap_token):
                self._json(HTTPStatus.FORBIDDEN, error="bootstrap unavailable")
                return
            self.server.bootstrap_used = True
            self.server.bootstrap_token = ""
            self.server.session_token = secrets.token_urlsafe(32)
            self.server.csrf_token = secrets.token_urlsafe(32)
        body = _json_bytes({"schema_version": SCHEMA_VERSION, "status": "success",
                            "data": {"csrf_token": self.server.csrf_token}})
        self.send_response(HTTPStatus.OK)
        self.send_header("Set-Cookie", f"{SESSION_COOKIE}={self.server.session_token}; HttpOnly; SameSite=Strict; Path=/")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _import_preview(self, data: dict[str, Any]) -> None:
        raw = self._decode_upload(data)
        source, account, kind = (str(data.get(key, "")).strip() for key in ("source", "account", "kind"))
        if not source or not account or not kind:
            raise ValueError("source, account and kind are required")
        preview = preview_capture(self.server.database, source=source, account=account,
                                  kind=kind, raw=raw, observed_at=data.get("observed_at"))
        draft = self.server.drafts.create(kind="capture", source=source, account=account,
                                          import_kind=kind, raw=raw, suffix=".capture", payload=preview,
                                          observed_at=data.get("observed_at"))
        self._json(HTTPStatus.OK, data={**preview, "draft_id": draft.draft_id})

    def _import_confirm(self, data: dict[str, Any]) -> None:
        draft = self.server.drafts.claim(str(data.get("draft_id", "")), str(data.get("evidence_sha256", "")))
        if draft.kind != "capture":
            self.server.drafts.consume(draft)
            raise ValueError("draft is not a structured capture")
        try:
            receipt = OperationalLedger(self.server.database).ingest_file(
                source=draft.source, account=draft.account, kind=draft.import_kind,
                input_path=draft.path, observed_at=draft.observed_at,
            )
        finally:
            self.server.drafts.consume(draft)
        self._json(HTTPStatus.OK, data=receipt)

    def _screenshot_draft(self, data: dict[str, Any]) -> None:
        raw = self._decode_upload(data)
        media_type = str(data.get("media_type", ""))
        validate_image(raw, media_type)
        source = str(data.get("source", "")).strip()
        account = str(data.get("account", "")).strip()
        if not source or not account:
            raise ValueError("source and account are required")
        suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[media_type]
        unknown_fields = [
            {"name": "observed_at", "value": None, "unit": None, "confidence": "0", "source_region": None, "unknown": True},
            {"name": "usage_or_balance", "value": None, "unit": None, "confidence": "0", "source_region": None, "unknown": True},
            {"name": "reset_at", "value": None, "unit": None, "confidence": "0", "source_region": None, "unknown": True},
        ]
        if media_type == "image/png":
            normalized, _width, _height = normalize_png_for_ocr(raw)
            draft_path = self.server.drafts.root / ("ocr_" + secrets.token_hex(16) + ".pgm")
            _write_private(draft_path, normalized)
            try:
                extraction = extract_screenshot(draft_path, source)
            finally:
                draft_path.unlink(missing_ok=True)
        else:
            extraction = {"extractor": {"id": "manual-review", "version": "0.1", "available": False},
                          "status": "manual_review_required", "fields": [], "text": None}
        payload = {"schema_version": SCHEMA_VERSION, "provider_template": source,
                   "extractor": extraction["extractor"],
                   "status": extraction["status"], "fields": extraction["fields"] or unknown_fields,
                   "ocr_text": extraction["text"],
                   "original_name": _safe_name(str(data.get("name", "capture")))}
        draft = self.server.drafts.create(kind="screenshot", source=source, account=account,
                                          import_kind="manual-snapshot", raw=raw, suffix=suffix,
                                          payload=payload)
        response = dict(payload)
        response.update({"draft_id": draft.draft_id, "image_sha256": draft.evidence_sha256,
                         "original_name": None})
        self._json(HTTPStatus.OK, data=response)

    def _screenshot_confirm(self, data: dict[str, Any]) -> None:
        draft = self.server.drafts.claim(str(data.get("draft_id", "")), str(data.get("evidence_sha256", "")))
        if draft.kind != "screenshot":
            self.server.drafts.consume(draft)
            raise ValueError("draft is not a screenshot")
        snapshot = data.get("snapshot")
        if not isinstance(snapshot, dict):
            self.server.drafts.consume(draft)
            raise ValueError("reviewed snapshot object is required")
        snapshot = dict(snapshot)
        expected_source = {"claude": "anthropic", "codex": "openai", "antigravity": "google"}.get(draft.source)
        if expected_source is None or snapshot.get("source") != expected_source or snapshot.get("account_alias") != draft.account:
            self.server.drafts.consume(draft)
            raise ValueError("reviewed snapshot does not match screenshot provider/account")
        try:
            record = parse_manual_template_json(snapshot)
            rows = export_canonical_measurements(record)
        except Exception:
            self.server.drafts.consume(draft)
            raise
        for row in rows:
            row["provenance"] = {"method": "screenshot_human_confirmation", "class": "P2", "confidence": "0.8"}
            row["dimensions"] = {**(row.get("dimensions") or {}),
                                 "screenshot_sha256": draft.evidence_sha256,
                                 "extractor_id": draft.payload["extractor"]["id"],
                                 "human_confirmed": True}
        retain = data.get("retain_original", False)
        if not isinstance(retain, bool):
            self.server.drafts.consume(draft)
            raise ValueError("retain_original must be a boolean")
        snapshot_path = draft.path.with_suffix(".json")
        _write_private(snapshot_path, _json_bytes({"rows": rows}) + b"\n")
        destination: Path | None = None
        try:
            if retain:
                destination = self.server.home / "evidence" / (secrets.token_hex(16) + draft.path.suffix)
                _write_private(destination, draft.path.read_bytes())
            receipt = OperationalLedger(self.server.database).ingest_file(
                source=draft.source, account=draft.account, kind="canonical",
                input_path=snapshot_path, observed_at=None,
                evidence_sha256_override=draft.evidence_sha256,
            )
        except Exception:
            if destination is not None:
                destination.unlink(missing_ok=True)
            raise
        finally:
            snapshot_path.unlink(missing_ok=True)
            self.server.drafts.consume(draft)
        self._json(HTTPStatus.OK, data=receipt)


def create_server(*, home: str | Path | None = None, database: str | Path | None = None,
                  bind: str = "127.0.0.1", port: int = 0) -> AELWebServer:
    if not _is_loopback(bind) or bind != "127.0.0.1":
        raise ValueError("MVP browser UI binds only to 127.0.0.1")
    setup = setup_workspace(home)
    resolved_home = Path(setup["paths"]["home"])
    resolved_database = resolve_database(database, home=resolved_home)
    OperationalLedger(resolved_database).initialize()
    return AELWebServer((bind, port), resolved_home, resolved_database)


def serve(*, home: str | Path | None = None, database: str | Path | None = None,
          bind: str = "127.0.0.1", port: int = 0, open_browser: bool = True) -> int:
    server = create_server(home=home, database=database, bind=bind, port=port)
    print(json.dumps({"schema_version": SCHEMA_VERSION, "status": "ready",
                      "url": server.launch_url, "bind": bind, "port": server.server_port},
                     sort_keys=True), flush=True)
    if open_browser:
        webbrowser.open(server.launch_url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def serve_in_thread(server: AELWebServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread
