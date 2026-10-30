import pytest
from derive.cost.allocation import allocate_cost

def test_allocation_conserves_total_and_remainder():
    result=allocate_cost("100","EUR",[{"target_id":"repo-a","amount":"40","evidence_ids":["sha:a"]}])
    assert result["allocated_total"]=="40"
    assert result["unallocated"]=="60"

def test_overallocation_and_partial_fx_are_rejected():
    with pytest.raises(ValueError,match="exceeds"):
        allocate_cost("10","EUR",[{"target_id":"x","amount":"11"}])
    with pytest.raises(ValueError,match="FX"):
        allocate_cost("10","EUR",[],fx={"rate":"1.1"})

