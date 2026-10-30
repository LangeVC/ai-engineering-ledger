import pytest
from collectors.conformance import fixture_observations, validate_manifest

def manifest():
    return {"schema_version":"0.4","adapter_id":"example","source":"example","surface":"subscription","capabilities":["usage"],"checkpoint_semantics":"cursor","evidence_states":["value","unknown"]}
def test_manifest_and_fixture_generation_are_credential_free_and_deterministic():
    assert validate_manifest(manifest())["adapter_id"]=="example"
    rows=[{"id":"2","value":2},{"id":"1","value":1}]
    assert fixture_observations("example",rows)==fixture_observations("example",list(reversed(rows)))
def test_secret_like_fixture_is_rejected():
    with pytest.raises(ValueError,match="secret"):
        fixture_observations("example",[{"id":"1","token":"no"}])

