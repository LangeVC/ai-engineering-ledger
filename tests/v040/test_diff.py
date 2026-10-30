from export.living.diff import build_diff

def manifest(identity): return {"status":"finalized","snapshot_id":identity}

def test_semantic_diff_is_order_stable_and_reasoned():
    before={"claims":[{"id":"a","value":1},{"id":"b","value":2}]}
    after={"claims":[{"value":2,"id":"b"},{"id":"a","value":3},{"id":"c","value":4}]}
    result=build_diff(manifest("snap_a"),manifest("snap_b"),before,after)
    kinds={(c["record_id"],c["change_type"],c["reason_code"]) for c in result["changes"]}
    assert any(k[1:] == ("changed","record_content_changed") for k in kinds)
    assert any(k[1:] == ("added","record_added") for k in kinds)
    assert len(result["content_hash"])==64

def test_diff_rejects_unfinalized_snapshot():
    import pytest
    with pytest.raises(ValueError,match="finalized"):
        build_diff({"status":"draft"},{"status":"finalized"}, {}, {})

def test_reordering_anonymous_semantic_values_is_not_a_change():
    before={"coverage":[{"state":"partial","reason":"gap"},{"state":"complete","reason":"ok"}]}
    after={"coverage":list(reversed(before["coverage"]))}
    result=build_diff(manifest("snap_a"),manifest("snap_b"),before,after)
    assert {item["change_type"] for item in result["changes"]} == {"unchanged"}

