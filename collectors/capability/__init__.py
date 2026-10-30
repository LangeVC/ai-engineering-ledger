"""Capability evidence normalization."""
from collectors.evidence_domains import normalize_domain_record
def normalize(record: dict) -> dict:
    payload=record.get("payload",{})
    if "maturity" in payload and "observed_use" not in payload:
        raise ValueError("capability maturity must not imply observed use")
    return normalize_domain_record("capability", record)

