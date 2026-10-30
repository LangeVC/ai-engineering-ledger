import json
from pathlib import Path

from collectors.claude_adapter import ClaudeAdapterConfig, ClaudeLocalAdapter


def write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def usage_row(output_tokens=5):
    return {
        "type": "assistant",
        "uuid": "envelope-001",
        "sessionId": "session-001",
        "timestamp": "2026-10-28T12:00:00Z",
        "message": {
            "id": "message-001",
            "model": "claude-sonnet-5-5",
            "content": [{"type": "text", "text": "must never be retained"}],
            "usage": {
                "input_tokens": 11,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": 101,
                "cache_creation_input_tokens": 7,
            },
        },
    }


def test_claude_dry_run_discovers_metadata_without_checkpoint_write(tmp_path):
    transcript = tmp_path / "projects" / "alpha" / "session.jsonl"
    checkpoint = tmp_path / "state" / "claude.json"
    write_jsonl(transcript, [usage_row()])

    result = ClaudeLocalAdapter(ClaudeAdapterConfig(checkpoint_path=checkpoint)).collect(
        [tmp_path / "projects"], dry_run=True
    )

    assert result["dry_run"] is True
    assert result["manifest"]["files_discovered"] == 1
    assert result["manifest"]["records_emitted"] == 1
    assert result["manifest"]["coverage"] == "partial"
    assert result["manifest"]["observed_from"] == "2026-10-28T12:00:00Z"
    assert not checkpoint.exists()


def test_claude_preserves_usage_but_excludes_prompts_and_content(tmp_path):
    transcript = tmp_path / "session.jsonl"
    write_jsonl(transcript, [usage_row()])

    result = ClaudeLocalAdapter().collect([transcript])
    record = result["records"][0]

    assert record["model"] == "claude-sonnet-5-5"
    assert record["request_id"] == "message-001"
    assert record["session_id"] == "session-001"
    assert record["input_tokens"] == 11
    assert record["output_tokens"] == 5
    assert record["cache_read_tokens"] == 101
    assert record["cache_creation_tokens"] == 7
    assert record["billing_assertion"] is False
    serialized = json.dumps(result)
    assert "must never be retained" not in serialized
    assert '"content"' not in serialized


def test_claude_checkpoint_makes_repeat_noop_and_changed_usage_revision(tmp_path):
    transcript = tmp_path / "session.jsonl"
    checkpoint = tmp_path / "claude.checkpoint.json"
    adapter = ClaudeLocalAdapter(ClaudeAdapterConfig(checkpoint_path=checkpoint))
    write_jsonl(transcript, [usage_row()])

    first = adapter.collect([transcript])
    second = adapter.collect([transcript])
    write_jsonl(transcript, [usage_row(output_tokens=8)])
    third = adapter.collect([transcript])

    assert len(first["records"]) == 1
    assert second["records"] == []
    assert second["manifest"]["unchanged_records"] == 1
    assert third["records"][0]["revision"]["number"] == 2
    assert third["records"][0]["revision"]["comparison"] == "content_changed"
    assert third["records"][0]["revision"]["supersedes"] == first["records"][0]["record_id"]


def test_claude_deduplicates_same_native_id_within_one_run(tmp_path):
    transcript = tmp_path / "session.jsonl"
    duplicate = usage_row()
    write_jsonl(transcript, [duplicate, duplicate])

    result = ClaudeLocalAdapter().collect([transcript])

    assert len(result["records"]) == 1
    assert result["records"][0]["revision"] == {
        "number": 1,
        "supersedes": None,
        "comparison": "new",
    }
    assert result["manifest"]["records_emitted"] == 1
    assert result["manifest"]["unchanged_records"] == 1
    assert result["manifest"]["files"][0]["accepted_records"] == 1


def test_claude_revisions_changed_same_native_id_within_one_run(tmp_path):
    transcript = tmp_path / "session.jsonl"
    checkpoint = tmp_path / "claude.checkpoint.json"
    write_jsonl(transcript, [usage_row(output_tokens=5), usage_row(output_tokens=8)])

    adapter = ClaudeLocalAdapter(ClaudeAdapterConfig(checkpoint_path=checkpoint))
    result = adapter.collect([transcript])
    repeated = adapter.collect([transcript])

    assert len(result["records"]) == 2
    first, second = result["records"]
    assert first["record_id"] != second["record_id"]
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
    assert result["checkpoint"]["session-001:message-001"]["record_id"] == second["record_id"]
    assert repeated["records"] == []
    assert repeated["manifest"]["unchanged_records"] == 2


def test_claude_unknown_and_invalid_lines_are_ignored_or_quarantined(tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "message": {"content": "secret"}}) + "\n" + "not-json\n",
        encoding="utf-8",
    )

    result = ClaudeLocalAdapter().collect([transcript])

    assert result["records"] == []
    assert len(result["manifest"]["quarantined"]) == 1
    assert "secret" not in json.dumps(result)
