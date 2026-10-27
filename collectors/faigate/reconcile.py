"""
Reconciliation Rule Engine for Gateway vs Provider Truth.
Complies with PRD US-10.c, US-15 (US-15.a - US-15.e) and TS Section 23.

Invariants:
- Gateway (faigate) is Execution Truth, NOT subscription or billing truth.
- Coverage = (observed / authoritative) * 100
- Provider authoritative 0 and Gateway 0 => 'kein_verbrauch' (no usage).
- Observed > 0 while Provider 0 => 'deviation_undefined_quotient'.
- Coverage > 100% is displayed unclipped and flagged as overcoverage/scope-investigation.
- Rounding occurs only in presentation / formatting.
"""

from dataclasses import dataclass
from typing import Optional, Union, Dict, Any


@dataclass
class ReconciliationMetric:
    name: str
    observed_gateway: Union[int, float]
    authoritative_provider: Union[int, float]
    delta: Union[int, float]  # observed - authoritative
    coverage_ratio: Optional[float]  # None if division by zero or not comparable
    status: str  # 'ok', 'no_usage', 'deviation_undefined', 'over_coverage', 'under_coverage', 'not_comparable'
    note: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "observed_gateway": self.observed_gateway,
            "authoritative_provider": self.authoritative_provider,
            "delta": self.delta,
            "coverage_ratio": self.coverage_ratio,
            "status": self.status,
            "note": self.note,
        }


def calculate_reconciliation(
    metric_name: str,
    observed: Union[int, float],
    authoritative: Union[int, float],
    is_comparable: bool = True,
    incomparable_reason: Optional[str] = None,
) -> ReconciliationMetric:
    """
    Computes reconciliation according to US-15 rules.
    """
    if not is_comparable:
        return ReconciliationMetric(
            name=metric_name,
            observed_gateway=observed,
            authoritative_provider=authoritative,
            delta=observed - authoritative,
            coverage_ratio=None,
            status="not_comparable",
            note=incomparable_reason or "Incompatible metric definitions or scope",
        )

    delta = observed - authoritative

    if authoritative == 0:
        if observed == 0:
            return ReconciliationMetric(
                name=metric_name,
                observed_gateway=0,
                authoritative_provider=0,
                delta=0,
                coverage_ratio=None,
                status="no_usage",
                note="Kein Verbrauch belegt (beide 0)",
            )
        else:
            return ReconciliationMetric(
                name=metric_name,
                observed_gateway=observed,
                authoritative_provider=0,
                delta=delta,
                coverage_ratio=None,
                status="deviation_undefined",
                note="Abweichung, Quotient undefiniert: Gateway beobachtet > 0 bei Provider 0",
            )

    ratio = observed / authoritative

    if ratio > 1.0:
        status = "over_coverage"
        note = f"Übererfassung/Scope-Problem prüfbar (Coverage: {ratio * 100:.4f}%)"
    elif ratio == 1.0:
        status = "ok"
        note = "Vollständige Deckung (100.0%)"
    else:
        status = "under_coverage"
        note = f"Untererfassung (Coverage: {ratio * 100:.4f}%)"

    return ReconciliationMetric(
        name=metric_name,
        observed_gateway=observed,
        authoritative_provider=authoritative,
        delta=delta,
        coverage_ratio=ratio,
        status=status,
        note=note,
    )
