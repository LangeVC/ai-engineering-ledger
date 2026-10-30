from __future__ import annotations

import http.client
import json
import sqlite3
from pathlib import Path

from ael.ledger import OperationalLedger
from ael.study_view import _analysis_from_ledger, dashboard_view_from_canonical
from ael.webui import create_server, serve_in_thread
from export.claims import generate_claim_registry
from export.study_json import build_study_json, normalize_study_filter


def _ingest(
    ledger: OperationalLedger,
    tmp_path: Path,
    *,
    source: str,
    account: str,
    metric: str,
    value: str | int,
    unit: str,
    observed_at: str,
    confidence: str = "1",
) -> None:
    capture = tmp_path / f"{source}-{metric}-{observed_at[:10]}.json"
    capture.write_text(
        json.dumps(
            [
                {
                    "source": source,
                    "source_account_id": account,
                    "metric": metric,
                    "value": value,
                    "unit": unit,
                    "aggregation": "interval_total",
                    "observed_at": observed_at,
                    "provenance": {
                        "class": "P1",
                        "method": "synthetic_test_fixture",
                        "confidence": confidence,
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    receipt = ledger.ingest_file(
        source=source,
        account=account,
        kind="canonical",
        input_path=capture,
    )
    assert receipt["accepted"] == 1


def test_full_and_bounded_ranges_share_one_half_open_filter(tmp_path: Path) -> None:
    ledger = OperationalLedger(tmp_path / "ledger.db")
    _ingest(
        ledger,
        tmp_path,
        source="claude",
        account="claude-pro",
        metric="input_tokens",
        value=100,
        unit="token",
        observed_at="2026-10-01T12:00:00Z",
        confidence="0.8",
    )
    _ingest(
        ledger,
        tmp_path,
        source="byteplus",
        account="byteplus-01",
        metric="settlement_amount",
        value="12.50",
        unit="USD",
        observed_at="2026-10-10T12:00:00Z",
    )
    _ingest(
        ledger,
        tmp_path,
        source="github",
        account="github-langevc",
        metric="commit_count",
        value=7,
        unit="commit",
        observed_at="2026-10-20T12:00:00Z",
    )

    full = ledger.study_report("2026-10-01", "2026-11-01")
    bounded = ledger.study_report("2026-10-01", "2026-10-20T12:00:00Z")

    assert full["observation_count"] == 3
    assert {name: len(records) for name, records in full["metric_families"].items()} == {
        "costs": 1,
        "usage": 1,
        "output": 1,
    }
    assert bounded["observation_count"] == 2
    assert bounded["metric_families"]["output"] == []
    assert len(full["claims"]) == len(full["groups"]) == 3
    assert all(
        claim["scope"]["dimensions"]["filter"] == full["filters"]["dimensions"]
        for claim in full["claims"]
    )
    assert all(claim["evidence_ids"] for claim in full["claims"])
    assert all("status" in claim["coverage"] for claim in full["claims"])
    assert all("level" in claim["confidence"] for claim in full["claims"])


def test_partial_and_non_comparable_data_produce_non_blocking_notices(tmp_path: Path) -> None:
    ledger = OperationalLedger(tmp_path / "ledger.db")
    _ingest(
        ledger,
        tmp_path,
        source="claude",
        account="claude-pro",
        metric="input_tokens",
        value=100,
        unit="token",
        observed_at="2026-10-01T12:00:00Z",
        confidence="0.6",
    )

    report = ledger.study_report("2026-10-01", "2026-11-01")

    assert report["claims"][0]["coverage"]["status"] == "partial"
    assert report["claims"][0]["coverage"]["warning_codes"] == ["PARTIAL_COVERAGE"]
    assert report["correlations"] == []
    assert any("confidence is partial" in notice for notice in report["notices"])
    assert any("coverage is unavailable" in notice for notice in report["notices"])


def test_dashboard_assets_bind_all_views_to_the_study_filter() -> None:
    html = Path("ael/assets/index.html").read_text(encoding="utf-8")
    javascript = Path("ael/assets/app.js").read_text(encoding="utf-8")

    assert 'id="study-filter"' in html
    assert 'name="start" type="date"' in html
    assert 'name="end_exclusive" type="date"' in html
    assert 'id="claim-list"' in html and 'id="correlation-list"' in html
    assert 'api(`/api/reports/study' in javascript
    assert 'query.set("start"' in javascript
    assert 'query.set("end_exclusive"' in javascript
    assert 'report.claims||[]' in javascript
    assert 'report.correlations||[]' in javascript


def test_dashboard_endpoint_applies_full_and_bounded_ranges(tmp_path: Path) -> None:
    server = create_server(home=tmp_path / "home")
    ledger = OperationalLedger(server.database)
    _ingest(
        ledger,
        tmp_path,
        source="claude",
        account="claude-pro",
        metric="input_tokens",
        value=100,
        unit="token",
        observed_at="2026-10-01T12:00:00Z",
    )
    _ingest(
        ledger,
        tmp_path,
        source="github",
        account="github-langevc",
        metric="commit_count",
        value=7,
        unit="commit",
        observed_at="2026-10-20T12:00:00Z",
    )
    thread = serve_in_thread(server)
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=4)
        body = json.dumps({"token": server.bootstrap_token})
        headers = {
            "Content-Type": "application/json",
            "Host": f"127.0.0.1:{server.server_port}",
            "Origin": server.origin,
        }
        connection.request("POST", "/api/session/bootstrap", body=body, headers=headers)
        response = connection.getresponse()
        response.read()
        assert response.status == 200
        cookie = response.getheader("Set-Cookie").split(";", 1)[0]
        connection.close()

        def get_report(path: str) -> dict:
            client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=4)
            client.request(
                "GET",
                path,
                headers={"Host": f"127.0.0.1:{server.server_port}", "Cookie": cookie},
            )
            result = client.getresponse()
            payload = json.loads(result.read())
            client.close()
            assert result.status == 200
            return payload["data"]

        full = get_report("/api/reports/study?start=2026-10-01&end_exclusive=2026-11-01")
        bounded = get_report(
            "/api/reports/study?start=2026-10-01&end_exclusive=2026-10-20T12%3A00%3A00Z"
        )
        assert full["observation_count"] == 2
        assert bounded["observation_count"] == 1
        assert normalize_study_filter(
            full["claims"][0]["scope"]["interval"], "Europe/Berlin"
        )["start"] == full["filters"]["start"]
        assert normalize_study_filter(
            bounded["claims"][0]["scope"]["interval"], "Europe/Berlin"
        )["start"] == bounded["filters"]["start"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def _canonical_analysis(start: str, end: str, *, unknown: bool = False) -> dict:
    return {
        "schema_version": "0.3",
        "contract": "ael.study.analysis",
        "transformation_version": "0.3.0",
        "filters": {"start": start, "end_exclusive": end},
        "timezone": "Europe/Berlin",
        "analysis_cutoff": end,
        "data_state": {"id": f"dataset-{start}-{end}"},
        "source_status": [],
        "source_gaps": [{"source": "claude", "reason": "provider history is partial"}],
        "transformations": [],
        "observation_phase_counts": {"reconstructed": 1, "prospective": 0, "unknown": 0},
        "evidence_hashes": [{"evidence_id": "ev-global", "sha256": "a" * 64}],
        "domains": {
            "runtime": {
                "status": "partial",
                "records": [
                    {
                        "period": start,
                        "state": "partial",
                        "value": 100,
                        "unit": "token",
                        "evidence_ids": ["ev-runtime"],
                        "warnings": ["local history only"],
                    }
                ],
            },
            "products": {"status": "unknown" if unknown else "unavailable", "records": []},
        },
    }


def _canonical_claims(analysis: dict) -> dict:
    return generate_claim_registry(
        analysis,
        claim_specs=[
            {"name": "runtime-total", "domain": "runtime", "operation": "sum", "unit": "token"},
            {"name": "products", "domain": "products", "operation": "sum", "unit": "product"},
        ],
        correlation_specs=[],
    )


def test_dashboard_is_a_projection_of_canonical_export_for_full_and_bounded_ranges() -> None:
    for start, end in (("2026-10-01", "2026-11-01"), ("2026-10-10", "2026-10-20")):
        analysis = _canonical_analysis(start, end, unknown=True)
        claims = _canonical_claims(analysis)
        active_filter = {"start": start, "end_exclusive": end}
        exported = build_study_json(
            analysis_bundle=analysis,
            claims_bundle=claims,
            active_filter=active_filter,
        )
        dashboard = dashboard_view_from_canonical(
            analysis_bundle=analysis,
            claims_bundle=claims,
            active_filter=active_filter,
        )

        assert dashboard["content_hash"] == exported["content_hash"]
        assert [
            (record["id"], record["value"], record["value_state"])
            for record in dashboard["claims"]
        ] == [
            (record["id"], record["value"], record["value_state"])
            for record in exported["claims"]
        ]
        unknown_claim = next(record for record in dashboard["claims"] if record["unit"] == "product")
        assert unknown_claim["value"] is None
        assert unknown_claim["value_state"] == "unknown"
        assert "provider history is partial" in dashboard["notices"]
        assert "products coverage is unknown" in dashboard["notices"]


def test_date_only_filter_uses_declared_timezone_and_dst_boundary(tmp_path: Path) -> None:
    ledger = OperationalLedger(tmp_path / "ledger.db")
    _ingest(
        ledger,
        tmp_path,
        source="claude",
        account="claude-pro",
        metric="input_tokens",
        value=1,
        unit="token",
        observed_at="2026-10-24T22:30:00Z",
    )
    _ingest(
        ledger,
        tmp_path,
        source="github",
        account="github-langevc",
        metric="commit_count",
        value=1,
        unit="commit",
        observed_at="2026-10-25T23:00:00Z",
    )

    report = ledger.study_report("2026-10-25", "2026-10-26")

    assert report["filters"]["start"] == "2026-10-24T22:00:00Z"
    assert report["filters"]["end_exclusive"] == "2026-10-25T23:00:00Z"
    assert report["observation_count"] == 1


def test_incomplete_source_gap_is_visible_even_with_full_confidence(tmp_path: Path) -> None:
    ledger = OperationalLedger(tmp_path / "ledger.db")
    _ingest(
        ledger,
        tmp_path,
        source="claude",
        account="claude-pro",
        metric="input_tokens",
        value=100,
        unit="token",
        observed_at="2026-10-01T12:00:00Z",
    )
    with sqlite3.connect(ledger.database) as connection:
        connection.execute("UPDATE import_receipts SET incomplete = 1")

    report = ledger.study_report("2026-10-01", "2026-11-01")

    assert report["domains"]["runtime"]["status"] == "partial"
    assert report["claims"][0]["coverage"]["status"] == "partial"
    assert "source import reported incomplete coverage" in report["notices"]


def test_known_incomplete_source_remains_visible_outside_active_interval(tmp_path: Path) -> None:
    ledger = OperationalLedger(tmp_path / "ledger.db")
    _ingest(
        ledger,
        tmp_path,
        source="claude",
        account="claude-pro",
        metric="input_tokens",
        value=100,
        unit="token",
        observed_at="2026-09-01T12:00:00Z",
    )
    with sqlite3.connect(ledger.database) as connection:
        connection.execute("UPDATE import_receipts SET incomplete = 1")

    report = ledger.study_report("2026-10-01", "2026-11-01")
    analysis, _, _ = _analysis_from_ledger(
        ledger.database,
        start="2026-10-01",
        end_exclusive="2026-11-01",
        timezone_name="Europe/Berlin",
    )

    assert report["observation_count"] == 0
    assert report["claims"] == []
    assert report["domains"]["runtime"] == {"status": "unavailable", "records": []}
    assert "source import reported incomplete coverage" in report["notices"]
    assert analysis["source_status"] == [
        {
            "source": "claude",
            "account": "claude-pro",
            "status": "partial",
            "reason": "source import reported incomplete coverage",
        }
    ]
    assert analysis["source_gaps"] == [
        {
            "source": "claude",
            "account": "claude-pro",
            "reason": "source import reported incomplete coverage",
        }
    ]
