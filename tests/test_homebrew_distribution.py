from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from ael.cli import main
from ael.homebrew import render
from ael.paths import resolve_database, resolve_home, setup_workspace, workspace_health


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_path_precedence_and_portable_defaults(tmp_path: Path) -> None:
    env = {"AEL_HOME": str(tmp_path / "env-home"), "AEL_DB_PATH": str(tmp_path / "custom.db")}
    assert resolve_home(environ=env) == tmp_path / "env-home"
    assert resolve_home(tmp_path / "explicit", env) == tmp_path / "explicit"
    assert resolve_database(environ=env) == tmp_path / "custom.db"
    assert resolve_database(tmp_path / "explicit.db", environ=env) == tmp_path / "explicit.db"
    assert resolve_home(environ={"XDG_DATA_HOME": str(tmp_path / "xdg")}) == tmp_path / "xdg" / "ael"


def test_setup_is_idempotent_private_and_preserves_data(tmp_path: Path) -> None:
    home = tmp_path / "ael-home"
    first = setup_workspace(home)
    database = Path(first["paths"]["database"])
    sentinel = home / "inbox" / "sentinel.json"
    sentinel.write_text("{}")
    os.chmod(sentinel, 0o600)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE persistence_probe (value TEXT NOT NULL)")
        connection.execute("INSERT INTO persistence_probe VALUES ('survives')")
    before = database.read_bytes()

    second = setup_workspace(home)
    assert second["paths"] == first["paths"]
    assert sentinel.read_text() == "{}"
    assert database.read_bytes() == before
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM persistence_probe").fetchone() == (
            "survives",
        )
    assert mode(home) == 0o700
    for name in ("inbox", "evidence", "exports", "backups"):
        assert mode(home / name) == 0o700
    assert mode(database) == 0o600
    assert workspace_health(home)["permissions_safe"] is True


def test_setup_refuses_symlinked_component(tmp_path: Path) -> None:
    home = tmp_path / "ael-home"
    home.mkdir()
    target = tmp_path / "outside"
    target.mkdir()
    (home / "inbox").symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinked"):
        setup_workspace(home)


def test_setup_refuses_symlinked_ancestor(tmp_path: Path) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinked"):
        setup_workspace(linked_parent / "ael-home")


def test_cli_defaults_to_ael_home(tmp_path: Path, monkeypatch, capsys) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("AEL_HOME", str(home))
    assert main(["setup"]) == 0
    setup_result = json.loads(capsys.readouterr().out)
    assert setup_result["paths"]["home"] == str(home)
    assert main(["doctor"]) == 0
    assert main(["paths"]) == 0
    paths_result = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert paths_result["paths"]["database"] == str(home / "ledger.db")


def test_formula_render_requires_release_inputs() -> None:
    template = Path("packaging/homebrew/ael.rb.in").read_text()
    formula = render(template, "0.1.0", "a" * 64, "Apache-2.0")
    assert "@VERSION@" not in formula
    assert "v0.1.0.tar.gz" in formula
    assert 'sha256 "' + "a" * 64 + '"' in formula
    assert 'license "Apache-2.0"' in formula
    assert "service do" not in formula
    assert "post_install_steps do" in formula
    assert 'run "ael", args: ["setup"' in formula
    with pytest.raises(ValueError):
        render(template, "v0.1.0", "a" * 64, "Apache-2.0")
