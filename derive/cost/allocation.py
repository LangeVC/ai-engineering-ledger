"""Conservative, replayable cost allocation with explicit remainder."""
from __future__ import annotations
from decimal import Decimal
from typing import Iterable, Mapping

def allocate_cost(total: str, currency: str, allocations: Iterable[Mapping[str,str]], *,
                  rule_version: str="0.4.0", fx: Mapping[str,str] | None=None) -> dict:
    source=Decimal(total)
    if len(currency)!=3: raise ValueError("currency must be ISO-4217 style")
    rows=[]; allocated=Decimal("0")
    for item in allocations:
        amount=Decimal(item["amount"])
        if amount<0: raise ValueError("allocation cannot be negative")
        allocated+=amount
        rows.append({"target_id":item["target_id"],"amount":format(amount,"f"),"evidence_ids":sorted(item.get("evidence_ids",[]))})
    if allocated>source: raise ValueError("allocated amount exceeds evidenced total")
    if fx is not None and not {"rate","date","source","target_currency"} <= set(fx):
        raise ValueError("FX conversion requires rate, date, source and target_currency")
    return {"schema_version":"0.4","rule_version":rule_version,"currency":currency,
            "source_total":format(source,"f"),"allocated":rows,
            "allocated_total":format(allocated,"f"),"unallocated":format(source-allocated,"f"),
            "fx_provenance":dict(fx) if fx else None}

