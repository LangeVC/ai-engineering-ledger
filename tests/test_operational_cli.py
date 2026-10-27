from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ael.cli import main
from ael.ledger import OperationalLedger


def write_json(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def import_args(db: Path, source: str, account: str, kind: str, path: Path, observed_at: str | None = None) -> list[str]:
    args = ["import", "--database", str(db), "--source", source, "--account", account,
            "--kind", kind, "--input", str(path)]
    if observed_at:
        args.extend(["--observed-at", observed_at])
    return args


def test_init_import_duplicate_and_report(tmp_path: Path, capsys) -> None:
    root = tmp_path / "ledger"
    assert main(["init", str(root)]) == 0
    db = root / "ledger.db"

    deepseek = write_json(tmp_path / "deepseek-balance.json", {
        "is_available": True,
        "balance_infos": [{"currency": "USD", "total_balance": "12.34", "granted_balance": "2.00"}],
    })
    args = import_args(db, "deepseek", "deepseek-main", "deepseek-balance", deepseek,
                       "2026-10-27T10:00:00+01:00")
    assert main(args) == 0
    first = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert first["accepted"] == 2
    assert first["persistence"] == "sqlite"
    assert "/Users/" not in json.dumps(first)

    assert main(args) == 0
    replay = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert replay["accepted"] == 0
    assert replay["existing"] == 2

    assert main(["report", "providers", "--database", str(db)]) == 0
    result = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert result["report"]["observation_count"] == 2
    assert {group["aggregation"] for group in result["report"]["groups"]} == {"gauge"}

    assert main(["doctor", str(root)]) == 0
    health = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert health["checks"]["schema_version"] == "0.2"
    assert health["checks"]["operational_tables"] is True


def test_manual_products_preserve_windows_and_credit_units(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    snapshots = [
        ("claude", "claude-pro", {"source": "anthropic", "product": "Claude Pro", "account_alias": "claude-pro",
            "observed_at": "2026-10-27T08:00:00Z", "quota_metrics": {"quota_remaining_percent": 80},
            "weekly_allotment": {"quota_remaining_percent": 60}}),
        ("codex", "codex-plus", {"source": "openai", "product": "ChatGPT Plus / Codex", "account_alias": "codex-plus",
            "observed_at": "2026-10-27T08:05:00Z", "allowance_metrics": {"quota_remaining_percent": 75},
            "weekly_allotment": {"quota_remaining_percent": 55}}),
        ("antigravity", "google-ai-pro", {"source": "google", "product": "Google Antigravity", "account_alias": "google-ai-pro",
            "observed_at": "2026-10-27T08:10:00Z", "credit_metrics": {"credit_balance": "9.5", "credits_consumed": "1.5"}}),
    ]
    for index, (source, account, payload) in enumerate(snapshots):
        path = write_json(tmp_path / f"manual-{index}.json", payload)
        assert main(import_args(db, source, account, "manual-snapshot", path)) == 0

    codex_second = write_json(tmp_path / "manual-codex-second.json", {
        "source": "openai", "product": "ChatGPT Plus / Codex", "account_alias": "codex-plus",
        "observed_at": "2026-10-27T09:05:00Z", "allowance_metrics": {"quota_remaining_percent": 65},
        "weekly_allotment": {"quota_remaining_percent": 45},
    })
    assert main(import_args(db, "codex", "codex-plus", "manual-snapshot", codex_second)) == 0

    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT source, account, metric, unit, window_type, window_label, reset_at FROM observations").fetchall()
    codex = [row for row in rows if row[0] == "openai"]
    assert any(row[4:6] == ("5h", "rolling_5h") for row in codex)
    assert all(row[6] is None for row in rows)
    credits = [row for row in rows if row[2] in {"credit_balance", "credits_consumed"}]
    assert credits and all(row[3] == "credit" for row in credits)
    report = OperationalLedger(db).provider_report()
    codex_quota = [group for group in report["groups"] if group["source"] == "openai" and group["metric"] == "quota_percent_used"]
    assert {group["window_type"] for group in codex_quota} == {"5h", "weekly"}
    assert {group["value"] for group in codex_quota} == {"35", "55"}
    assert {group["observation_count"] for group in codex_quota} == {2}


def test_deepseek_csv_byteplus_accounts_and_kilo_scopes(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    csv_path = tmp_path / "deepseek.csv"
    csv_path.write_text(
        "date,model,requests,prompt_tokens,prompt_cache_hit_tokens,prompt_cache_miss_tokens,completion_tokens,reasoning_tokens,cost\n"
        "2026-10-26,deepseek-chat,2,100,40,60,20,5,0.0123\n",
        encoding="utf-8",
    )
    assert main(import_args(db, "deepseek", "deepseek-main", "deepseek-csv", csv_path)) == 0

    for account, used in (("bp01", 10), ("bp05", 50)):
        plan = write_json(tmp_path / f"{account}.json", {
            "quota_limit": 1000, "quota_used": used, "quota_remaining": 1000 - used,
            "quota_unit": "request", "plan": "coding_plan_pro",
        })
        assert main(import_args(db, "byteplus", account, "byteplus-plan", plan,
                                "2026-10-27T09:00:00Z")) == 0

    session_fixture = Path("collectors/kilo/tests/fixtures/valid_session_synthetic.json")
    stats_fixture = Path("collectors/kilo/tests/fixtures/valid_aggregate_stats.json")
    assert main(import_args(db, "kilo", "kilo-local", "kilo-session", session_fixture)) == 0
    assert main(import_args(db, "kilo", "kilo-local", "kilo-stats", stats_fixture,
                            "2026-10-27T09:30:00Z")) == 0

    report = OperationalLedger(db).provider_report()
    accounts = {group["account"] for group in report["groups"]}
    assert {"bp01", "bp05", "deepseek-main", "kilo-local"}.issubset(accounts)
    kilo_scopes = {group["scope"] for group in report["groups"] if group["source"] == "kilo"}
    assert "aggregate" in kilo_scopes
    assert "session" in kilo_scopes or "request" in kilo_scopes


def test_truncated_byteplus_billing_is_partial_and_persisted(tmp_path: Path, capsys) -> None:
    db = tmp_path / "ledger.db"
    billing = write_json(tmp_path / "billing.json", {
        "page": 7,
        "data": {
            "is_truncated": True,
            "items": [{"settlement_amount": "12.50", "input_tokens": 100, "record_id": 12345}],
        },
    })
    code = main(import_args(db, "byteplus", "bp03", "byteplus-billing", billing,
                            "2026-10-27T10:00:00Z"))
    assert code == 3
    receipt = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert receipt["incomplete"] is True
    assert receipt["status"] == "partial"
    report = OperationalLedger(db).provider_report()
    metrics = {group["metric"]: group["value"] for group in report["groups"]}
    assert metrics == {"input_tokens": "100", "settlement_amount": "12.50"}


def test_invalid_manual_percentage_is_rejected_without_observation(tmp_path: Path, capsys) -> None:
    db = tmp_path / "ledger.db"
    bad = write_json(tmp_path / "bad.json", {
        "source": "anthropic", "product": "Claude Pro", "account_alias": "claude-pro",
        "observed_at": "2026-10-27T08:00:00Z", "quota_metrics": {"quota_remaining_percent": 101},
    })
    code = main(import_args(db, "claude", "claude-pro", "manual-snapshot", bad))
    assert code == 3
    receipt = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert receipt["accepted"] == 0 and receipt["rejected"] == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0


def test_canonical_account_mismatch_is_rejected_without_relabeling(tmp_path: Path, capsys) -> None:
    db = tmp_path / "ledger.db"
    capture = write_json(tmp_path / "canonical.json", [{
        "source": "byteplus", "source_account_id": "bp02", "metric": "input_tokens",
        "value": 10, "unit": "token", "aggregation": "interval_total",
        "observed_at": "2026-10-27T08:00:00Z",
    }])
    code = main(import_args(db, "byteplus", "bp01", "canonical", capture))
    assert code == 3
    receipt = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert receipt["accepted"] == 0 and receipt["rejected"] == 1
    assert "does not match --account" in receipt["errors"][0]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
