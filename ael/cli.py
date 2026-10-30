from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ael import __version__
from ael.ledger import OperationalLedger, utc_now
from ael.paths import resolve_database, setup_workspace, workspace_health, workspace_paths
from ael.study_view import build_ledger_study_json
from ael.webui import serve
from collectors.orchestration.jobs import CollectionJobStore
from export.living import (
    SnapshotRegistry,
    SnapshotVerification,
    build_changelog,
    build_diff,
    build_living_export,
    build_snapshot,
)
from export.privacy import PrivacyProfile, scan_artifacts
from export.study import StudyExporter
from reconcile.coverage import ComparableScope, compare
from storage.recovery import RecoveryManager


def _emit(status: str, command: str, **details: object) -> int:
    code = {"success": 0, "partial": 3, "failure": 1}[status]
    print(json.dumps({"schema_version": "0.1", "status": status, "exit_code": code,
                      "command": command, **details}, sort_keys=True))
    return code


def _init(args: argparse.Namespace) -> int:
    result = setup_workspace(args.directory)
    return _emit("success", "init", directory=result["paths"]["home"], warnings=[])


def _setup(args: argparse.Namespace) -> int:
    result = setup_workspace(args.home)
    return _emit("success", "setup", **result)


def _paths(args: argparse.Namespace) -> int:
    resolved = workspace_paths(args.home)
    return _emit("success", "paths", paths={key: str(value) for key, value in resolved.items()})


def _doctor(args: argparse.Namespace) -> int:
    checks = workspace_health(args.directory)
    return _emit("success" if all(checks.values()) else "failure", "doctor", checks=checks)


def _snapshot(args: argparse.Namespace) -> int:
    record = {
        "source": args.source, "account": args.account, "metric": args.metric,
        "value": args.value, "unit": args.unit, "observed_at": args.observed_at,
        "provenance": {"method": "manual", "confidence": args.confidence},
    }
    Path(args.output).write_text(json.dumps(record, sort_keys=True, indent=2) + "\n")
    os.chmod(args.output, 0o600)
    return _emit("success", "snapshot", output=args.output)


def _ingest(args: argparse.Namespace) -> int:
    receipt = OperationalLedger(resolve_database(args.database)).ingest_file(
        source=args.source,
        account=args.account,
        kind=args.kind,
        input_path=args.input,
        observed_at=args.observed_at,
    )
    status = "success" if receipt["rejected"] == 0 and not receipt["incomplete"] else "partial"
    return _emit(status, args.command, **receipt, persistence="sqlite")


def _collect_dispatch(args: argparse.Namespace) -> int:
    if args.collect_action != "run":
        missing = [name for name in ("source", "account", "input", "kind") if not getattr(args, name)]
        if missing:
            raise ValueError("legacy collect requires " + ", ".join("--" + name for name in missing))
        return _ingest(args)
    database = resolve_database(args.database)
    ledger = OperationalLedger(database)
    ledger.initialize()
    jobs = CollectionJobStore(database)
    due = [job.to_contract() for job in jobs.due(now=args.at or utc_now())]
    return _emit("success", "collect run", due=due, due_count=len(due),
                 execution="dry_run" if args.dry_run else "requires_registered_adapter_runtime")


def _report_providers(args: argparse.Namespace) -> int:
    report = OperationalLedger(resolve_database(args.database)).provider_report(args.from_time, args.to_time)
    if args.format == "table":
        print("source\taccount\tmetric\tunit\tscope\taggregation\tvalue\tcount\tlast")
        for group in report["groups"]:
            print("\t".join(str(group[key]) for key in (
                "source", "account", "metric", "unit", "scope", "aggregation",
                "value", "observation_count", "last_observed_at",
            )))
        return 0
    return _emit("success", "report providers", report=report)


def _reconcile(args: argparse.Namespace) -> int:
    scope = ComparableScope(args.from_time, args.to_time, args.account, args.product,
                            args.metric_definition, args.aggregation, tuple(args.routing_subset))
    result = compare(
        {"value": args.provider_value, "revision": args.provider_revision},
        {"value": args.runtime_value, "revision": args.runtime_revision},
        scope, scope,
    )
    return _emit("success", "reconcile", result=result)


def _delete_preview(args: argparse.Namespace) -> int:
    records = json.loads(Path(args.input).read_text())
    preview = RecoveryManager().deletion_preview(records, args.account)
    return _emit("success", "delete-preview", preview=preview)


def _delete(args: argparse.Namespace) -> int:
    if args.confirm != args.account:
        raise ValueError("--confirm must exactly match --account")
    source = Path(args.input)
    records = json.loads(source.read_text())
    remaining, tombstone = RecoveryManager().delete(records, args.account, True)
    source.write_text(json.dumps(remaining, sort_keys=True, indent=2) + "\n")
    os.chmod(source, 0o600)
    return _emit("success", "delete", tombstone=tombstone,
                 external_cleanup=["backups", "previous exports"])


def _export(args: argparse.Namespace) -> int:
    input_data = json.loads(Path(args.input).read_text()) if args.input else {}
    manifest = StudyExporter().write(
        args.output, input_data.get("tables", {}),
        filters={"from": args.from_date, "to": args.to_date},
        timezone_name=args.timezone, study_cutoff=args.study_cutoff,
        data_state=input_data.get("data_state", {"id": "ds-" + "0" * 8, "as_of": args.study_cutoff}),
        source_status=input_data.get("source_status", []), missing_periods=input_data.get("missing_periods", []),
        confidence_distribution=input_data.get("confidence_distribution", {}),
        evidence_hashes=input_data.get("evidence_hashes", []),
    )
    return _emit("success", "export", content_hash=manifest["content_hash"], output=args.output)


def _backup(args: argparse.Namespace) -> int:
    config = json.loads(Path(args.config).read_text()) if args.config else {}
    manifest = RecoveryManager().backup(args.output, resolve_database(args.database), config, args.evidence)
    return _emit("success", "backup", files=len(manifest["files"]))


def _restore(args: argparse.Namespace) -> int:
    manifest = RecoveryManager().restore(args.backup, args.output)
    return _emit("success", "restore", files=len(manifest["files"]))


def _ui(args: argparse.Namespace) -> int:
    return serve(home=args.home, database=args.database, bind=args.bind,
                 port=args.port, open_browser=not args.no_open)


def _living_root(home: str | None) -> Path:
    return Path(workspace_paths(home)["home"]) / "living-study"


def _source_health(database: Path) -> list[dict[str, object]]:
    import sqlite3
    if not database.is_file():
        return []
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "source_health" not in tables:
            return []
        rows = connection.execute(
            """SELECT h.* FROM source_health h JOIN (
                 SELECT source_alias, MAX(evaluated_at) AS evaluated_at
                 FROM source_health GROUP BY source_alias
               ) latest ON latest.source_alias=h.source_alias AND latest.evaluated_at=h.evaluated_at
               ORDER BY h.source_alias"""
        ).fetchall()
    return [{key: row[key] for key in ("source_alias", "state", "reason_code", "rule_version",
                                        "evaluated_at", "last_attempt_at", "last_success_at")} for row in rows]


def _study_build(args: argparse.Namespace) -> int:
    setup_workspace(args.home)
    database = resolve_database(args.database)
    OperationalLedger(database).initialize()
    study = build_ledger_study_json(database, start=args.start, end_exclusive=args.end_exclusive)
    artifacts = {"study.json": study}
    aliases = sorted({str(group["account"]) for group in study.get("groups", [])})
    privacy = scan_artifacts(artifacts, profile=PrivacyProfile.create("living-study", aliases))
    if not privacy["passed"]:
        raise ValueError("study build failed privacy gate")
    registry = SnapshotRegistry(_living_root(args.home))
    parent = None
    try:
        parent = registry.latest()["snapshot_id"]
    except FileNotFoundError:
        pass
    draft = build_snapshot(
        artifacts,
        filter_spec=study["active_filter"],
        transformation_version="0.4.0",
        parent_snapshot_id=parent,
        warnings=tuple(study.get("notices", [])),
    )
    manifest = registry.finalize(draft, SnapshotVerification.valid())
    return _emit("success", "study build", snapshot_id=manifest["snapshot_id"],
                 content_hash=manifest["content_hash"], warnings=manifest["warnings"])


def _study_list(args: argparse.Namespace) -> int:
    root = _living_root(args.home)
    snapshots = SnapshotRegistry(root).list() if root.is_dir() else []
    return _emit("success", "study list", snapshots=snapshots)


def _snapshot_data(root: Path, snapshot_id: str) -> tuple[dict[str, object], dict[str, object]]:
    registry = SnapshotRegistry(root)
    manifest = registry.verify(snapshot_id)
    path = root / "snapshots" / snapshot_id / "study.json"
    return manifest, json.loads(path.read_text())


def _study_diff(args: argparse.Namespace) -> int:
    root = _living_root(args.home)
    before_manifest, before = _snapshot_data(root, args.from_snapshot)
    after_manifest, after = _snapshot_data(root, args.to_snapshot)
    diff = build_diff(before_manifest, after_manifest, before, after)
    if args.output:
        Path(args.output).write_text(json.dumps(diff, sort_keys=True, indent=2) + "\n")
        os.chmod(args.output, 0o600)
    return _emit("success", "study diff", diff=diff, output=args.output)


def _study_export(args: argparse.Namespace) -> int:
    root = _living_root(args.home)
    registry = SnapshotRegistry(root)
    snapshot_id = args.snapshot
    if snapshot_id == "latest":
        snapshot_id = registry.latest()["snapshot_id"]
    manifest, study = _snapshot_data(root, snapshot_id)
    diff = None
    if args.from_snapshot:
        previous_manifest, previous = _snapshot_data(root, args.from_snapshot)
        diff = build_diff(previous_manifest, manifest, previous, study)
    living = build_living_export(
        study_v03=study,
        snapshot=manifest,
        source_health=_source_health(resolve_database(args.database)),
        diff=diff,
        changelog=build_changelog(diff) if diff else [],
    )
    Path(args.output).write_text(json.dumps(living, sort_keys=True, indent=2) + "\n")
    os.chmod(args.output, 0o600)
    return _emit("success", "study export", output=args.output,
                 snapshot_id=snapshot_id, content_hash=living["content_hash"])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ael")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init"); init.add_argument("directory"); init.set_defaults(run=_init)
    setup = sub.add_parser("setup"); setup.add_argument("--home"); setup.set_defaults(run=_setup)
    paths = sub.add_parser("paths"); paths.add_argument("--home"); paths.set_defaults(run=_paths)
    doctor = sub.add_parser("doctor"); doctor.add_argument("directory", nargs="?"); doctor.set_defaults(run=_doctor)
    snap = sub.add_parser("snapshot")
    for name in ("source", "account", "metric", "value", "unit", "observed-at", "output"):
        snap.add_argument("--" + name, required=True)
    snap.add_argument("--confidence", type=float, default=0.5); snap.set_defaults(run=_snapshot)
    collect = sub.add_parser("collect")
    collect.add_argument("collect_action", nargs="?", choices=["run"])
    collect.add_argument("--source", choices=["deepseek", "byteplus", "claude", "codex", "antigravity", "kilo", "faigate", "github", "forgejo"])
    collect.add_argument("--account"); collect.add_argument("--input")
    collect.add_argument("--database"); collect.add_argument("--kind")
    collect.add_argument("--observed-at"); collect.add_argument("--at"); collect.add_argument("--dry-run", action="store_true")
    collect.set_defaults(run=_collect_dispatch)
    imp = sub.add_parser("import")
    imp.add_argument("--database"); imp.add_argument("--source", required=True)
    imp.add_argument("--account", required=True); imp.add_argument("--kind", required=True,
        choices=["canonical", "manual-snapshot", "deepseek-csv", "deepseek-balance", "deepseek-usage",
                 "byteplus-plan", "byteplus-stats", "byteplus-billing", "kilo-session", "kilo-stats"])
    imp.add_argument("--input", required=True); imp.add_argument("--observed-at"); imp.set_defaults(run=_ingest)
    report = sub.add_parser("report")
    report_sub = report.add_subparsers(dest="report_command", required=True)
    providers = report_sub.add_parser("providers")
    providers.add_argument("--database")
    providers.add_argument("--from", dest="from_time"); providers.add_argument("--to", dest="to_time")
    providers.add_argument("--format", choices=["json", "table"], default="json")
    providers.set_defaults(run=_report_providers)
    rec = sub.add_parser("reconcile")
    rec.add_argument("--provider-value", required=True); rec.add_argument("--runtime-value", required=True)
    rec.add_argument("--provider-revision"); rec.add_argument("--runtime-revision")
    rec.add_argument("--from", dest="from_time", required=True); rec.add_argument("--to", dest="to_time", required=True)
    rec.add_argument("--account", required=True); rec.add_argument("--product", required=True)
    rec.add_argument("--metric-definition", required=True); rec.add_argument("--aggregation", default="sum")
    rec.add_argument("--routing-subset", action="append", default=[]); rec.set_defaults(run=_reconcile)
    export = sub.add_parser("export"); export.add_argument("--input"); export.add_argument("--output", required=True)
    export.add_argument("--from", dest="from_date", required=True); export.add_argument("--to", dest="to_date", required=True)
    export.add_argument("--timezone", default="UTC"); export.add_argument("--study-cutoff", required=True); export.set_defaults(run=_export)
    backup = sub.add_parser("backup"); backup.add_argument("--database"); backup.add_argument("--config")
    backup.add_argument("--evidence", action="append", default=[]); backup.add_argument("--output", required=True); backup.set_defaults(run=_backup)
    restore = sub.add_parser("restore"); restore.add_argument("--backup", required=True); restore.add_argument("--output", required=True); restore.set_defaults(run=_restore)
    ui = sub.add_parser("ui")
    ui.add_argument("--home"); ui.add_argument("--database")
    ui.add_argument("--bind", default="127.0.0.1"); ui.add_argument("--port", type=int, default=0)
    ui.add_argument("--no-open", action="store_true"); ui.set_defaults(run=_ui)
    study = sub.add_parser("study")
    study_sub = study.add_subparsers(dest="study_command", required=True)
    study_build = study_sub.add_parser("build")
    study_build.add_argument("--home"); study_build.add_argument("--database")
    study_build.add_argument("--start"); study_build.add_argument("--end-exclusive")
    study_build.set_defaults(run=_study_build)
    study_list = study_sub.add_parser("list"); study_list.add_argument("--home"); study_list.set_defaults(run=_study_list)
    study_diff = study_sub.add_parser("diff")
    study_diff.add_argument("--home"); study_diff.add_argument("--from-snapshot", required=True)
    study_diff.add_argument("--to-snapshot", required=True); study_diff.add_argument("--output")
    study_diff.set_defaults(run=_study_diff)
    study_export = study_sub.add_parser("export")
    study_export.add_argument("--home"); study_export.add_argument("--database")
    study_export.add_argument("--snapshot", default="latest"); study_export.add_argument("--from-snapshot")
    study_export.add_argument("--output", required=True); study_export.set_defaults(run=_study_export)
    preview = sub.add_parser("delete-preview"); preview.add_argument("--input", required=True); preview.add_argument("--account", required=True); preview.set_defaults(run=_delete_preview)
    delete = sub.add_parser("delete"); delete.add_argument("--input", required=True); delete.add_argument("--account", required=True)
    delete.add_argument("--confirm", required=True); delete.set_defaults(run=_delete)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return args.run(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"schema_version": "0.1", "status": "failure", "exit_code": 1,
                          "error": type(exc).__name__, "message": str(exc)}), file=sys.stdout)
        return 1
