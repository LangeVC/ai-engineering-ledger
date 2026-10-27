"""Portable, security-conscious workspace resolution for AEL."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping

from ael.ledger import OperationalLedger


WORKSPACE_DIRS = ("inbox", "evidence", "exports", "backups")


def resolve_home(explicit: str | os.PathLike[str] | None = None,
                 environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    if explicit:
        return Path(explicit).expanduser()
    if env.get("AEL_HOME"):
        return Path(env["AEL_HOME"]).expanduser()
    if env.get("XDG_DATA_HOME"):
        return Path(env["XDG_DATA_HOME"]).expanduser() / "ael"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ael"
    return Path.home() / ".local" / "share" / "ael"


def resolve_database(explicit: str | os.PathLike[str] | None = None,
                     home: str | os.PathLike[str] | None = None,
                     environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    if explicit:
        return Path(explicit).expanduser()
    if env.get("AEL_DB_PATH"):
        return Path(env["AEL_DB_PATH"]).expanduser()
    return resolve_home(home, env) / "ledger.db"


def workspace_paths(home: str | os.PathLike[str] | None = None,
                    environ: Mapping[str, str] | None = None) -> dict[str, Path]:
    root = resolve_home(home, environ)
    return {
        "home": root,
        "database": resolve_database(home=root, environ=environ),
        **{name: root / name for name in WORKSPACE_DIRS},
    }


def _refuse_symlink_chain(path: Path) -> None:
    absolute = path.expanduser().absolute()
    for component in reversed((absolute, *absolute.parents)):
        if component.is_symlink():
            raise ValueError(
                f"refusing symlinked AEL workspace path component: {component}"
            )


def setup_workspace(home: str | os.PathLike[str] | None = None,
                    environ: Mapping[str, str] | None = None) -> dict[str, object]:
    paths = workspace_paths(home, environ)
    root = paths["home"]
    assert isinstance(root, Path)
    _refuse_symlink_chain(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _refuse_symlink_chain(root)
    os.chmod(root, 0o700)
    for name in WORKSPACE_DIRS:
        directory = paths[name]
        assert isinstance(directory, Path)
        _refuse_symlink_chain(directory)
        directory.mkdir(exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
    database = paths["database"]
    assert isinstance(database, Path)
    _refuse_symlink_chain(database)
    OperationalLedger(database).initialize()
    os.chmod(database, 0o600)
    return {
        "paths": {key: str(value) for key, value in paths.items()},
        "modes": {"directories": "0700", "database": "0600"},
    }


def workspace_health(home: str | os.PathLike[str] | None = None,
                     environ: Mapping[str, str] | None = None) -> dict[str, object]:
    paths = workspace_paths(home, environ)
    root = paths["home"]
    database = paths["database"]
    assert isinstance(root, Path) and isinstance(database, Path)
    directory_modes: dict[str, str | None] = {}
    safe = not root.is_symlink()
    for name in ("home", *WORKSPACE_DIRS):
        path = paths[name]
        assert isinstance(path, Path)
        mode = oct(path.stat().st_mode & 0o777) if path.is_dir() else None
        directory_modes[name] = mode
        safe = safe and not path.is_symlink() and mode == "0o700"
    database_mode = oct(database.stat().st_mode & 0o777) if database.is_file() else None
    safe = safe and not database.is_symlink() and database_mode == "0o600"
    ledger = OperationalLedger(database).health()
    return {
        "workspace_exists": root.is_dir(),
        "directory_modes": directory_modes,
        "database_mode": database_mode,
        "permissions_safe": safe,
        **ledger,
    }
