"""Correlation rules for PRD US-11.

Only explicit source references and user-confirmed mappings are authoritative.
Heuristics remain candidates and never silently alter project totals.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable


@dataclass(frozen=True)
class Correlation:
    subject_id: str
    project_id: str | None
    strength: str
    evidence_id: str | None = None

    def __post_init__(self) -> None:
        if self.strength not in {"explicit_source", "user_confirmed", "heuristic", "unmapped"}:
            raise ValueError("unsupported correlation strength")
        if self.strength in {"explicit_source", "user_confirmed"} and not self.project_id:
            raise ValueError("confirmed correlations require project_id")
        if self.strength == "explicit_source" and not self.evidence_id:
            raise ValueError("explicit source correlations require evidence")

    @property
    def confirmed(self) -> bool:
        return self.strength in {"explicit_source", "user_confirmed"}


class CorrelationLedger:
    def __init__(self, correlations: Iterable[Correlation] = ()) -> None:
        self._by_subject = {c.subject_id: c for c in correlations}

    def resolve(self, subject_id: str) -> Correlation:
        return self._by_subject.get(subject_id, Correlation(subject_id, None, "unmapped"))

    def partition(self, rows: Iterable[dict[str, Any]], project_id: str | None = None) -> dict[str, Any]:
        matched, unassigned, candidates = [], [], []
        for row in rows:
            corr = self.resolve(str(row["id"]))
            enriched = {**row, "correlation": corr}
            if corr.confirmed:
                if project_id is None or corr.project_id == project_id:
                    matched.append(enriched)
            elif corr.strength == "heuristic":
                candidates.append(enriched)
                unassigned.append(enriched)
            else:
                unassigned.append(enriched)
        return {
            "matched": matched,
            "unassigned": unassigned,
            "heuristic_candidates": candidates,
            "unassigned_count": len(unassigned),
            "project_filter_excludes_unassigned": bool(project_id and unassigned),
        }


def aggregate_council(parent: dict[str, Any], children: Iterable[dict[str, Any]], level: str) -> dict[str, Any]:
    """Return exactly one summing level; the other is retained as a non-additive view."""
    if level not in {"parent", "children"}:
        raise ValueError("level must be parent or children")
    kids = list(children)
    if any(c.get("council_id") != parent.get("council_id") for c in kids):
        raise ValueError("council scope mismatch")
    if level == "parent":
        total = Decimal(str(parent.get("tokens", 0)))
        contributors = [parent.get("id")]
        view = kids
    else:
        total = sum((Decimal(str(c.get("tokens", 0))) for c in kids), Decimal("0"))
        contributors = [c.get("id") for c in kids]
        view = [parent]
    return {
        "tokens": str(total),
        "summing_level": level,
        "contributors": contributors,
        "non_additive_view": view,
    }
