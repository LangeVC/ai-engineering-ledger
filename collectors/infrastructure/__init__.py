"""Infrastructure/deployment evidence normalization."""
from collectors.evidence_domains import normalize_domain_record
def normalize(record: dict) -> dict: return normalize_domain_record("infrastructure", record)

