from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

from ael.ledger import OperationalLedger
from ael.webui import AELRequestHandler, MappingStore
from export.living import SnapshotRegistry, SnapshotVerification, build_snapshot


def handler(home: Path, database: Path) -> AELRequestHandler:
    value = object.__new__(AELRequestHandler)
    value.server = SimpleNamespace(home=home, database=database, mappings=MappingStore(home))
    return value


def test_living_view_projects_registry_and_latest_health_without_recalculation(tmp_path: Path) -> None:
    database = tmp_path / "ael.db"
    OperationalLedger(database).initialize()
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO source_health VALUES (?,?,?,?,?,?,?,?,?)""",
            ("h1", "deepseek-main", "stale", "cadence_elapsed", "0.4.0",
             "2026-10-30T12:00:00Z", "2026-10-30T11:00:00Z",
             "2026-10-29T11:00:00Z", "{}"),
        )
    registry = SnapshotRegistry(tmp_path / "living-study")
    draft = build_snapshot(
        {"study.json": {"schema_version": "0.3", "claims": []}},
        filter_spec={"start": "2026-04-26", "end_exclusive": "2026-11-01"},
        transformation_version="0.4.0",
        warnings=("partial coverage",),
    )
    manifest = registry.finalize(draft, SnapshotVerification.valid())
    view = handler(tmp_path, database)._living_view(None)
    assert view["contract"] == "ael.local-ui.living-study@0.4"
    assert view["latest_snapshot_id"] == manifest["snapshot_id"]
    assert view["snapshots"][0]["content_hash"] == manifest["content_hash"]
    assert view["source_health"][0]["state"] == "stale"
    assert view["warnings"] == ["deepseek-main: cadence_elapsed"]


def test_living_ui_is_accessible_data_projection() -> None:
    root = Path(__file__).parents[2] / "ael" / "assets"
    html = (root / "index.html").read_text()
    script = (root / "app.js").read_text()
    assert 'data-view="living"' in html
    assert 'id="living-health"' in html
    assert 'role="status"' in html
    assert "/api/living" in script
    assert "content_hash" in script


def test_mapping_edits_append_revisions_without_rewriting_history(tmp_path: Path) -> None:
    store = MappingStore(tmp_path)
    first = store.append({"mapping_id": "provider-to-repo", "source_alias": "deepseek-main",
                          "repository_alias": "github-langevc"})
    second = store.append({"mapping_id": "provider-to-repo", "source_alias": "deepseek-main",
                           "repository_alias": "forgejo-langevc"})
    assert first["revision"] == 1
    assert second["revision"] == 2
    assert first["revision_id"] != second["revision_id"]
    assert store.load() == [first, second]

