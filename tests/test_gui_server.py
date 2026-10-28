from __future__ import annotations

import base64
import http.client
import json
import sqlite3
from pathlib import Path

import pytest

from ael.webui import AliasStore, create_server, extract_screenshot, normalize_png_for_ocr, serve_in_thread


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class Client:
    def __init__(self, server):
        self.server = server
        self.cookie = ""
        self.csrf = ""

    def request(self, method: str, path: str, payload=None, *, auth=True, origin=True, host=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=4)
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"Host": host or f"127.0.0.1:{self.server.server_port}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if origin:
            headers["Origin"] = self.server.origin
        if auth and self.cookie:
            headers["Cookie"] = self.cookie
            headers["X-AEL-CSRF"] = self.csrf
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        result = json.loads(raw) if raw and response.getheader("Content-Type", "").startswith("application/json") else raw
        set_cookie = response.getheader("Set-Cookie")
        if set_cookie:
            self.cookie = set_cookie.split(";", 1)[0]
        connection.close()
        return response.status, result, dict(response.getheaders())

    def bootstrap(self):
        status, result, _ = self.request("POST", "/api/session/bootstrap",
                                         {"token": self.server.bootstrap_token}, auth=False)
        assert status == 200
        self.csrf = result["data"]["csrf_token"]


@pytest.fixture
def running(tmp_path: Path):
    server = create_server(home=tmp_path / "home")
    thread = serve_in_thread(server)
    try:
        yield server, Client(server), tmp_path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


def test_loopback_only_and_security_headers(tmp_path: Path, running) -> None:
    with pytest.raises(ValueError, match="127.0.0.1"):
        create_server(home=tmp_path / "bad", bind="0.0.0.0")
    server, client, _ = running
    status, _, headers = client.request("GET", "/", auth=False, origin=False)
    assert status == 200
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    status, _, _ = client.request("GET", "/", auth=False, origin=False, host="evil.example")
    assert status == 421


def test_bootstrap_single_use_and_mutation_guards(running) -> None:
    server, client, _ = running
    token = server.bootstrap_token
    client.bootstrap()
    status, _, _ = client.request("POST", "/api/session/bootstrap", {"token": token}, auth=False)
    assert status == 403
    status, resumed, _ = client.request("GET", "/api/session", origin=False)
    assert status == 200 and resumed["data"]["csrf_token"] == client.csrf
    status, _, _ = client.request("POST", "/api/config/aliases", {"alias": {"id": "x", "provider": "deepseek"}}, auth=False)
    assert status == 403
    status, _, _ = client.request("POST", "/api/config/aliases", {"alias": {"id": "x", "provider": "deepseek"}}, origin=False)
    assert status == 403


def test_aliases_reject_secrets_and_isolate_five_byteplus(running) -> None:
    _server, client, _ = running
    client.bootstrap()
    for index in range(1, 6):
        status, result, _ = client.request("POST", "/api/config/aliases", {
            "alias": {"id": f"bp0{index}", "provider": "byteplus", "cadence": "manual",
                      "secret_ref": f"keychain:ael/bp0{index}"}
        })
        assert status == 200 and result["data"]["id"] == f"bp0{index}"
    status, result, _ = client.request("GET", "/api/config", origin=False)
    assert status == 200
    assert {item["id"] for item in result["data"]["aliases"]} == {f"bp0{i}" for i in range(1, 6)}
    status, _, _ = client.request("POST", "/api/config/aliases", {
        "alias": {"id": "bad", "provider": "deepseek", "api_key": "sentinel-secret"}
    })
    assert status == 400


def test_structured_preview_is_zero_write_and_confirm_matches_ledger(running) -> None:
    server, client, _ = running
    client.bootstrap()
    raw = json.dumps({"balance_infos": [{"currency": "USD", "total_balance": "12.5"}]}).encode()
    payload = {"source": "deepseek", "account": "deepseek-main", "kind": "deepseek-balance",
               "content_base64": base64.b64encode(raw).decode()}
    status, preview, _ = client.request("POST", "/api/import/preview", payload)
    assert status == 200 and preview["data"]["accepted"] == 1
    with sqlite3.connect(server.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM import_receipts").fetchone()[0] == 0
    status, confirmed, _ = client.request("POST", "/api/import/confirm", {
        "draft_id": preview["data"]["draft_id"],
        "evidence_sha256": preview["data"]["evidence_sha256"],
    })
    assert status == 200 and confirmed["data"]["accepted"] == 1
    status, _, _ = client.request("POST", "/api/import/confirm", {
        "draft_id": preview["data"]["draft_id"],
        "evidence_sha256": preview["data"]["evidence_sha256"],
    })
    assert status == 400


def test_screenshot_creates_review_draft_without_ledger_write(running) -> None:
    server, client, _ = running
    client.bootstrap()
    # Magic-valid synthetic PNG evidence; OCR may reject pixels but must degrade safely.
    raw = PNG_1X1
    status, result, _ = client.request("POST", "/api/screenshots/draft", {
        "source": "claude", "account": "claude-pro", "name": "../../dashboard.html",
        "media_type": "image/png", "content_base64": base64.b64encode(raw).decode(),
    })
    assert status == 200
    data = result["data"]
    assert data["status"] == "manual_review_required"
    assert data["original_name"] is None
    with sqlite3.connect(server.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
    status, _, _ = client.request("POST", "/api/drafts/discard", {
        "draft_id": data["draft_id"], "evidence_sha256": data["image_sha256"]
    })
    assert status == 200
    assert not list((server.home / "inbox" / "gui-staging").iterdir())


def test_screenshot_confirmation_persists_image_and_human_provenance(running) -> None:
    server, client, _ = running
    client.bootstrap()
    status, result, _ = client.request("POST", "/api/screenshots/draft", {
        "source": "claude", "account": "claude-pro", "name": "dashboard.png",
        "media_type": "image/png", "content_base64": base64.b64encode(PNG_1X1).decode(),
    })
    assert status == 200
    draft = result["data"]
    snapshot = {"source": "anthropic", "product": "Claude Pro", "account_alias": "claude-pro",
                "observed_at": "2026-10-28T12:00:00Z",
                "quota_metrics": {"quota_remaining_percent": 73}}
    status, confirmed, _ = client.request("POST", "/api/screenshots/confirm", {
        "draft_id": draft["draft_id"], "evidence_sha256": draft["image_sha256"],
        "snapshot": snapshot, "retain_original": False,
    })
    assert status == 200 and confirmed["data"]["accepted"] >= 1
    with sqlite3.connect(server.database) as connection:
        method, confidence, evidence, dimensions = connection.execute(
            "SELECT provenance_method, confidence, evidence_sha256, dimensions_json FROM observations LIMIT 1"
        ).fetchone()
    assert method == "screenshot_human_confirmation"
    assert confidence == "0.8"
    assert evidence == draft["image_sha256"]
    assert json.loads(dimensions)["human_confirmed"] is True
    assert not list((server.home / "inbox" / "gui-staging").iterdir())


def test_preview_time_is_immutable_and_intra_file_duplicates_match_confirmation(running) -> None:
    server, client, _ = running
    client.bootstrap()
    row = {"source": "generic", "source_account_id": "acct", "metric": "requests",
           "value": 1, "unit": "request", "aggregation": "interval_total"}
    raw = json.dumps([row, row]).encode()
    status, preview, _ = client.request("POST", "/api/import/preview", {
        "source": "generic", "account": "acct", "kind": "canonical",
        "observed_at": "2026-10-28T12:00:00Z", "content_base64": base64.b64encode(raw).decode(),
    })
    assert status == 200
    assert (preview["data"]["accepted"], preview["data"]["existing"]) == (1, 1)
    expected_key = preview["data"]["observations"][0]["idempotency_key"]
    status, confirmed, _ = client.request("POST", "/api/import/confirm", {
        "draft_id": preview["data"]["draft_id"], "evidence_sha256": preview["data"]["evidence_sha256"],
        "observed_at": "2030-01-01T00:00:00Z",
    })
    assert status == 200
    assert (confirmed["data"]["accepted"], confirmed["data"]["existing"]) == (1, 1)
    with sqlite3.connect(server.database) as connection:
        assert connection.execute("SELECT idempotency_key FROM observations").fetchone()[0] == expected_key


def test_modified_staged_capture_is_refused(running) -> None:
    server, client, _ = running
    client.bootstrap()
    raw = json.dumps({"balance_infos": [{"currency": "USD", "total_balance": "4"}]}).encode()
    status, preview, _ = client.request("POST", "/api/import/preview", {
        "source": "deepseek", "account": "main", "kind": "deepseek-balance",
        "content_base64": base64.b64encode(raw).decode(),
    })
    assert status == 200
    draft = server.drafts._drafts[preview["data"]["draft_id"]]
    draft.path.write_bytes(b"tampered")
    status, _, _ = client.request("POST", "/api/import/confirm", {
        "draft_id": preview["data"]["draft_id"],
        "evidence_sha256": preview["data"]["evidence_sha256"],
    })
    assert status == 400
    with sqlite3.connect(server.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0


def test_media_type_spoof_is_rejected(running) -> None:
    _server, client, _ = running
    client.bootstrap()
    status, _, _ = client.request("POST", "/api/screenshots/draft", {
        "source": "codex", "account": "codex-plus", "name": "fake.png",
        "media_type": "image/png", "content_base64": base64.b64encode(b"<script>x</script>").decode(),
    })
    assert status == 400


def test_png_crc_corruption_is_rejected_before_ocr(running) -> None:
    _server, client, _ = running
    client.bootstrap()
    corrupt = bytearray(PNG_1X1)
    corrupt[45] ^= 0x01
    status, _, _ = client.request("POST", "/api/screenshots/draft", {
        "source": "claude", "account": "claude-pro", "name": "corrupt.png",
        "media_type": "image/png", "content_base64": base64.b64encode(corrupt).decode(),
    })
    assert status == 400


def test_png_is_fully_decoded_and_normalized_before_ocr() -> None:
    pgm, width, height = normalize_png_for_ocr(PNG_1X1)
    assert (width, height) == (1, 1)
    assert pgm.startswith(b"P5\n1 1\n255\n") and len(pgm) == len(b"P5\n1 1\n255\n") + 1


def test_optional_tesseract_returns_unclassified_candidates(tmp_path: Path, monkeypatch) -> None:
    image = tmp_path / "capture.png"
    image.write_bytes(b"synthetic")
    monkeypatch.setattr("ael.webui.shutil.which", lambda _name: "/opt/local/bin/tesseract")

    class Result:
        returncode = 0
        stdout = "Weekly remaining 72%\nBalance 18.5 credits\n"

    monkeypatch.setattr("ael.webui.subprocess.run", lambda *args, **kwargs: Result())
    result = extract_screenshot(image, "claude")
    assert result["extractor"]["id"] == "tesseract"
    assert result["status"] == "manual_review_required"
    assert {field["name"] for field in result["fields"]} == {
        "unclassified_percent", "unclassified_credit"
    }


def test_alias_store_refuses_symlinked_config(tmp_path: Path) -> None:
    target = tmp_path / "outside.json"
    target.write_text('{"schema_version":"0.1","aliases":[]}')
    home = tmp_path / "home"
    home.mkdir()
    (home / "gui-config.json").symlink_to(target)
    with pytest.raises(ValueError, match="symlinked"):
        AliasStore(home).load()
