from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from ael.ledger import OperationalLedger
from ael.paths import resolve_database, setup_workspace, workspace_health, workspace_paths
from ael.webui import serve
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ael")
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
    collect.add_argument("--source", required=True, choices=["deepseek", "byteplus", "claude", "codex", "antigravity", "kilo", "faigate", "github", "forgejo"])
    collect.add_argument("--account", required=True); collect.add_argument("--input", required=True)
    collect.add_argument("--database"); collect.add_argument("--kind", required=True)
    collect.add_argument("--observed-at"); collect.set_defaults(run=_ingest)
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
