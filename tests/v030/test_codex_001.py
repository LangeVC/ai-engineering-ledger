import json
from pathlib import Path

import pytest

from collectors.codex_adapter import CodexAdapterConfig, CodexLocalAdapter


def write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def direct_record(output_tokens=5):
    return {
        "id": "request-001",
        "session_id": "session-001",
        "turn_id": "turn-001",
        "timestamp": "2026-10-28T13:00:00Z",
        "model": "gpt-6.1-sol",
        "prompt": "must not be retained",
        "usage": {
            "input_tokens": 13,
            "output_tokens": output_tokens,
            "reasoning_tokens": 3,
            "cached_input_tokens": 21,
        },
    }


def token_count_event():
    return {
        "timestamp": "2026-10-28T14:00:00Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "session_id": "session-002",
            "turn_id": "turn-002",
            "request_id": "request-002",
            "info": {
                "model": "gpt-6.1-sol",
                "last_token_usage": {
                    "input_tokens": 8,
                    "output_tokens": 2,
                    "reasoning_tokens": 1,
                    "cached_input_tokens": 4,
                },
            },
        },
    }


def test_codex_parses_supported_direct_and_event_usage_without_billing_claim(tmp_path):
    path = tmp_path / "sessions" / "rollout.jsonl"
    write_jsonl(path, [direct_record(), token_count_event()])

    result = CodexLocalAdapter().collect([tmp_path / "sessions"])

    assert result["capability"]["status"] == "supported"
    assert result["manifest"]["coverage"] == "partial"
    assert len(result["records"]) == 2
    assert {record["request_id"] for record in result["records"]} == {"request-001", "request-002"}
    assert all(record["billing_assertion"] is False for record in result["records"])
    assert all(record["semantic_family"] == "raw_token_activity" for record in result["records"])
    assert "must not be retained" not in json.dumps(result)


def test_codex_total_usage_is_labeled_session_total_and_not_plus_allowance(tmp_path):
    path = tmp_path / "rollout.jsonl"
    write_jsonl(
        path,
        [
            {
                "timestamp": "2026-10-28T14:00:00Z",
                "session_id": "session-003",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 80,
                            "output_tokens": 20,
                            "reasoning_tokens": 10,
                            "cached_input_tokens": 40,
                        }
                    },
                },
            }
        ],
    )

    record = CodexLocalAdapter().collect([path])["records"][0]
    assert record["scope_level"] == "session_total"
    assert record["plan"] == "plus"
    assert record["billing_assertion"] is False
    assert "allowance" not in record
    assert "credits" not in record


def test_codex_checkpoint_noop_and_content_revision(tmp_path):
    path = tmp_path / "rollout.jsonl"
    checkpoint = tmp_path / "codex.checkpoint.json"
    adapter = CodexLocalAdapter(CodexAdapterConfig(checkpoint_path=checkpoint))
    write_jsonl(path, [direct_record()])

    first = adapter.collect([path])
    second = adapter.collect([path])
    write_jsonl(path, [direct_record(output_tokens=9)])
    third = adapter.collect([path])

    assert len(first["records"]) == 1
    assert second["records"] == []
    assert second["manifest"]["unchanged_records"] == 1
    assert third["records"][0]["revision"]["number"] == 2
    assert third["records"][0]["revision"]["supersedes"] == first["records"][0]["record_id"]


def test_codex_idless_records_are_file_stable_collision_free_and_revisioned(tmp_path):
    sessions = tmp_path / "sessions"
    first_path = sessions / "first.jsonl"
    second_path = sessions / "second.jsonl"
    checkpoint = tmp_path / "codex.checkpoint.json"
    adapter = CodexLocalAdapter(CodexAdapterConfig(checkpoint_path=checkpoint))

    first_record = direct_record(output_tokens=5)
    second_record = direct_record(output_tokens=7)
    for record in (first_record, second_record):
        record.pop("id")
        record.pop("session_id")
        record.pop("turn_id")
    write_jsonl(first_path, [first_record])
    write_jsonl(second_path, [second_record])

    first = adapter.collect([sessions])
    second = adapter.collect([sessions])
    second_record["usage"]["output_tokens"] = 9
    write_jsonl(second_path, [second_record])
    third = adapter.collect([sessions])

    assert len(first["records"]) == 2
    assert len({record["request_id"] for record in first["records"]}) == 2
    assert len(first["checkpoint"]) == 2
    assert second["records"] == []
    assert second["manifest"]["unchanged_records"] == 2
    assert len(third["records"]) == 1
    assert third["records"][0]["revision"]["number"] == 2
    first_by_request = {record["request_id"]: record for record in first["records"]}
    changed_request = third["records"][0]["request_id"]
    assert third["records"][0]["revision"]["supersedes"] == first_by_request[changed_request]["record_id"]
    assert len(third["checkpoint"]) == 2
    rendered = json.dumps(first)
    assert str(tmp_path) not in rendered


def test_codex_exact_duplicate_native_id_is_emitted_once_per_collect(tmp_path):
    path = tmp_path / "rollout.jsonl"
    checkpoint = tmp_path / "codex.checkpoint.json"
    adapter = CodexLocalAdapter(CodexAdapterConfig(checkpoint_path=checkpoint))
    write_jsonl(path, [direct_record(), direct_record()])

    result = adapter.collect([path])

    assert len(result["records"]) == 1
    assert result["manifest"]["unchanged_records"] == 1
    assert result["manifest"]["records_emitted"] == 1
    assert len(result["checkpoint"]) == 1


def test_codex_changed_native_id_revisions_in_order_within_one_collect(tmp_path):
    path = tmp_path / "rollout.jsonl"
    checkpoint = tmp_path / "codex.checkpoint.json"
    adapter = CodexLocalAdapter(CodexAdapterConfig(checkpoint_path=checkpoint))
    write_jsonl(path, [direct_record(output_tokens=5), direct_record(output_tokens=9)])

    result = adapter.collect([path])

    assert len(result["records"]) == 2
    first, second = result["records"]
    assert first["revision"] == {
        "number": 1,
        "supersedes": None,
        "comparison": "new",
    }
    assert second["revision"] == {
        "number": 2,
        "supersedes": first["record_id"],
        "comparison": "content_changed",
    }
    assert first["record_id"] != second["record_id"]
    assert result["checkpoint"]["session-001:turn-001:request-001"]["record_id"] == second[
        "record_id"
    ]


def test_codex_full_replay_of_multi_version_native_id_is_noop(tmp_path):
    path = tmp_path / "rollout.jsonl"
    checkpoint = tmp_path / "codex.checkpoint.json"
    adapter = CodexLocalAdapter(CodexAdapterConfig(checkpoint_path=checkpoint))
    write_jsonl(path, [direct_record(output_tokens=5), direct_record(output_tokens=9)])

    first = adapter.collect([path])
    second = adapter.collect([path])

    assert [record["revision"]["number"] for record in first["records"]] == [1, 2]
    assert second["records"] == []
    assert second["manifest"]["unchanged_records"] == 2
    state = second["checkpoint"]["session-001:turn-001:request-001"]
    assert state["revision"] == 2
    assert len(state["versions"]) == 2
    assert {version["revision"] for version in state["versions"].values()} == {1, 2}


def test_codex_legacy_checkpoint_is_upgraded_when_content_changes(tmp_path):
    path = tmp_path / "rollout.jsonl"
    checkpoint = tmp_path / "codex.checkpoint.json"
    adapter = CodexLocalAdapter(CodexAdapterConfig(checkpoint_path=checkpoint))
    write_jsonl(path, [direct_record(output_tokens=5)])
    first = adapter.collect([path])
    key = "session-001:turn-001:request-001"
    legacy = first["checkpoint"][key]
    legacy.pop("versions")
    checkpoint.write_text(
        json.dumps(
            {
                "schema_version": "0.3",
                "parser_version": "0.3.0",
                "records": {key: legacy},
            }
        ),
        encoding="utf-8",
    )
    write_jsonl(path, [direct_record(output_tokens=9)])

    changed = adapter.collect([path])

    state = changed["checkpoint"][key]
    assert changed["records"][0]["revision"]["number"] == 2
    assert changed["records"][0]["revision"]["supersedes"] == legacy["record_id"]
    assert len(state["versions"]) == 2
    assert legacy["content_hash"] in state["versions"]


def test_codex_reports_unavailable_or_unsupported_coverage(tmp_path):
    missing = CodexLocalAdapter().collect([tmp_path / "missing"])
    assert missing["capability"]["status"] == "unavailable"
    assert missing["manifest"]["coverage"] == "unavailable"

    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({"session": {"id": "without-usage"}}), encoding="utf-8")
    unsupported = CodexLocalAdapter().collect([path])
    assert unsupported["capability"]["status"] == "unsupported_shape"
    assert unsupported["manifest"]["unsupported_shapes"] == 1


def test_codex_plus_and_api_key_modes_cannot_share_identity():
    with pytest.raises(ValueError, match="separate source identities"):
        CodexAdapterConfig(account_alias="codex-plus", auth_mode="api_key", plan="plus")

    api = CodexAdapterConfig(account_alias="codex-api", auth_mode="api_key", plan="api")
    assert api.account_alias != CodexAdapterConfig().account_alias
