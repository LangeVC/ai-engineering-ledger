import pytest
from collectors.process import normalize as process
from collectors.infrastructure import normalize as infrastructure
from collectors.capability import normalize as capability

BASE={"interval":{"start":"2026-10-30T10:00:00Z","end_exclusive":"2026-10-30T11:00:00Z"},"confidence":1,"coverage":"complete","provenance":{"method":"fixture","evidence_ids":["sha:a"]},"payload":{}}
def test_three_domains_are_provenance_complete():
    assert process(BASE)["domain"]=="process"
    assert infrastructure(BASE)["domain"]=="infrastructure"
    cap={**BASE,"payload":{"maturity":"operational","observed_use":True}}
    assert capability(cap)["payload"]["observed_use"] is True

def test_maturity_never_implies_observed_use():
    with pytest.raises(ValueError,match="observed use"):
        capability({**BASE,"payload":{"maturity":"operational"}})

