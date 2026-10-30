import json
import pytest
from export.living import SnapshotRegistry, SnapshotVerification, build_snapshot, verify_snapshot

def test_replay_verifies_hash_and_privacy(tmp_path):
    draft=build_snapshot({"study.json":{"alias":"deepseek-main","value":1}},filter_spec={"start":"a","end_exclusive":"b"},transformation_version="0.4.0")
    manifest=SnapshotRegistry(tmp_path).finalize(draft,SnapshotVerification.valid())
    result=verify_snapshot(tmp_path,manifest["snapshot_id"],approved_aliases=["deepseek-main"])
    assert result["replay"]=="passed"
    assert result["content_hash"]==manifest["content_hash"]

def test_privacy_blocks_prompt_content(tmp_path):
    draft=build_snapshot({"study.json":{"prompt":"private"}},filter_spec={"start":"a","end_exclusive":"b"},transformation_version="0.4.0")
    manifest=SnapshotRegistry(tmp_path).finalize(draft,SnapshotVerification.valid())
    with pytest.raises(ValueError,match="privacy"):
        verify_snapshot(tmp_path,manifest["snapshot_id"])

