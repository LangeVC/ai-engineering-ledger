"""Correlation rules for PRD US-11.

Only explicit source references and user-confirmed mappings are authoritative.
Heuristics remain candidates and never silently alter project totals.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
from typing import Any, Iterable


ASSOCIATION_KINDS = {"task", "session", "repository", "change", "release"}
ASSOCIATION_METHODS = {
    "explicit_id",
    "user_confirmed",
    "mapping_rule",
    "temporal_candidate",
    "unmapped",
}
ASSOCIATION_STRENGTHS = {"strong", "medium", "weak", "none"}
AUTHORITATIVE_METHODS = {"explicit_id", "user_confirmed", "mapping_rule"}


def _association_id(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "link_" + hashlib.sha256(encoded).hexdigest()[:24]


@dataclass(frozen=True)
class AssociationLink:
    """One evidence-bearing, explicitly non-causal relationship.

    A temporal match is deliberately represented as a candidate.  It can be
    inspected but it never becomes an attributed/confirmed link without a
    stronger method being recorded as a new link.
    """

    subject_kind: str
    subject_id: str
    object_kind: str
    object_id: str | None
    association_method: str
    strength: str
    evidence_ids: tuple[str, ...] = ()
    method_version: str = "0.4.0"

    def __post_init__(self) -> None:
        if self.subject_kind not in ASSOCIATION_KINDS:
            raise ValueError("unsupported association subject kind")
        if self.object_kind not in ASSOCIATION_KINDS:
            raise ValueError("unsupported association object kind")
        if not self.subject_id:
            raise ValueError("association subject_id is required")
        if self.association_method not in ASSOCIATION_METHODS:
            raise ValueError("unsupported association method")
        if self.strength not in ASSOCIATION_STRENGTHS:
            raise ValueError("unsupported association strength")
        if not self.method_version:
            raise ValueError("association method_version is required")
        canonical_evidence = tuple(sorted(set(self.evidence_ids)))
        object.__setattr__(self, "evidence_ids", canonical_evidence)

        if self.association_method == "unmapped":
            if self.object_id is not None or self.strength != "none":
                raise ValueError("unmapped association must have no object and strength none")
        elif not self.object_id:
            raise ValueError("mapped association requires object_id")

        if self.association_method in AUTHORITATIVE_METHODS and not canonical_evidence:
            raise ValueError("authoritative association requires evidence")
        if self.association_method == "explicit_id" and self.strength != "strong":
            raise ValueError("explicit ID association must be strong")
        if self.association_method == "temporal_candidate" and self.strength not in {"weak", "none"}:
            raise ValueError("temporal candidate cannot be medium or strong")

    @property
    def link_id(self) -> str:
        return _association_id(
            {
                "subject_kind": self.subject_kind,
                "subject_id": self.subject_id,
                "object_kind": self.object_kind,
                "object_id": self.object_id,
                "association_method": self.association_method,
                "strength": self.strength,
                "evidence_ids": list(self.evidence_ids),
                "method_version": self.method_version,
            }
        )

    @property
    def confirmed(self) -> bool:
        return self.association_method in AUTHORITATIVE_METHODS

    def as_record(self) -> dict[str, Any]:
        return {
            "schema_version": "0.4",
            "record_type": "association_link",
            "link_id": self.link_id,
            "subject": {"kind": self.subject_kind, "id": self.subject_id},
            "object": (
                {"kind": self.object_kind, "id": self.object_id}
                if self.object_id is not None
                else {"kind": self.object_kind, "id": None}
            ),
            "association_method": self.association_method,
            "strength": self.strength,
            "evidence_ids": list(self.evidence_ids),
            "method_version": self.method_version,
            "confirmed": self.confirmed,
            "interpretation": "association_only",
        }


class AssociationLedger:
    """Deterministic many-to-many index for v0.4 association links."""

    def __init__(self, links: Iterable[AssociationLink] = ()) -> None:
        unique = {link.link_id: link for link in links}
        self._links = tuple(unique[key] for key in sorted(unique))

    def links_for(self, kind: str, identity: str) -> tuple[AssociationLink, ...]:
        return tuple(
            link
            for link in self._links
            if (link.subject_kind == kind and link.subject_id == identity)
            or (link.object_kind == kind and link.object_id == identity)
        )

    def records(self) -> list[dict[str, Any]]:
        return [link.as_record() for link in self._links]

    def partition(self) -> dict[str, list[dict[str, Any]]]:
        confirmed, candidates, unmapped = [], [], []
        for link in self._links:
            record = link.as_record()
            if link.confirmed:
                confirmed.append(record)
            elif link.association_method == "temporal_candidate":
                candidates.append(record)
            else:
                unmapped.append(record)
        return {
            "confirmed": confirmed,
            "candidates": candidates,
            "unmapped": unmapped,
        }


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
