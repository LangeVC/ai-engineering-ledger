"""
Faigate Collector and Adapter Module (Stdlib Python 3, sqlite3).
Implements execution observation collection for faigate (fusionAIze Gate) Schema 2.10.2.
Complies with PRD US-10, US-15 and TS Section 23.
"""

from .adapter import FaigateAdapter, FaigateConfig, FaigateRequestRecord, CollectionResult, ReconciliationResult
from .reconcile import calculate_reconciliation, ReconciliationMetric

__all__ = [
    "FaigateAdapter",
    "FaigateConfig",
    "FaigateRequestRecord",
    "CollectionResult",
    "ReconciliationResult",
    "calculate_reconciliation",
    "ReconciliationMetric",
]
