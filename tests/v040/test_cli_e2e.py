from __future__ import annotations

import json
from pathlib import Path

from ael.cli import main


def last_json(capsys) -> dict:
    return json.loads(capsys.readouterr().out.splitlines()[-1])


def import_balance(db: Path, path: Path, value: str, observed_at: str) -> None:
    path.write_text(json.dumps({"is_available": True, "balance_infos": [{"currency": "USD", "total_balance": value, "granted_balance": "0"}]}))
    assert main(["import", "--database", str(db), "--source", "deepseek", "--account", "deepseek-main",
                 "--kind", "deepseek-balance", "--input", str(path), "--observed-at", observed_at]) == 0


def test_collect_build_list_diff_export_fixture_e2e(tmp_path: Path, capsys) -> None:
    home = tmp_path / "ael-home"
    assert main(["setup", "--home", str(home)]) == 0
    capsys.readouterr()
    db = home / "ledger.db"

    assert main(["collect", "run", "--database", str(db), "--dry-run", "--at", "2026-10-30T12:00:00Z"]) == 0
    assert last_json(capsys)["due_count"] == 0

    import_balance(db, tmp_path / "first.json", "10", "2026-10-29T10:00:00Z")
    capsys.readouterr()
    assert main(["study", "build", "--home", str(home), "--database", str(db),
                 "--start", "2026-10-29", "--end-exclusive", "2026-10-31"]) == 0
    first = last_json(capsys)["snapshot_id"]

    import_balance(db, tmp_path / "second.json", "8", "2026-10-30T10:00:00Z")
    capsys.readouterr()
    assert main(["study", "build", "--home", str(home), "--database", str(db),
                 "--start", "2026-10-29", "--end-exclusive", "2026-10-31"]) == 0
    second = last_json(capsys)["snapshot_id"]
    assert first != second

    assert main(["study", "list", "--home", str(home)]) == 0
    assert len(last_json(capsys)["snapshots"]) == 2
    diff_path = tmp_path / "diff.json"
    assert main(["study", "diff", "--home", str(home), "--from-snapshot", first,
                 "--to-snapshot", second, "--output", str(diff_path)]) == 0
    change_types = {item["change_type"] for item in json.loads(diff_path.read_text())["changes"]}
    assert {"added", "removed"} <= change_types or "changed" in change_types

    output = tmp_path / "living.json"
    assert main(["study", "export", "--home", str(home), "--database", str(db),
                 "--snapshot", second, "--from-snapshot", first, "--output", str(output)]) == 0
    exported = json.loads(output.read_text())
    assert exported["contract"] == "ael.study.data-export@0.4"
    assert exported["snapshot"]["snapshot_id"] == second
    assert exported["diff"]["from_snapshot_id"] == first
    assert output.stat().st_mode & 0o777 == 0o600


def test_legacy_collect_parser_contract_remains_available(tmp_path: Path, capsys) -> None:
    db = tmp_path / "ledger.db"
    source = tmp_path / "balance.json"
    import_balance(db, source, "1", "2026-10-30T10:00:00Z")
    result = last_json(capsys)
    assert result["command"] == "import"
    assert result["accepted"] == 2

