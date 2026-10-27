"""Cross-field rules not expressible in the JSON Schema subset. Each returns a list of error strings."""
import json, math
from decimal import Decimal

def load_json_strict(text):
    def bad(c): raise ValueError(f"non-finite constant {c}")
    return json.loads(text, parse_constant=bad)

def check_measurement(m, catalog):
    e = []
    spec = catalog["metrics"].get(m.get("metric"))
    if spec is None: return ["unknown metric"]
    if m.get("unit") not in spec["units"]: e.append(f"unit {m.get('unit')} not allowed for {m['metric']}")
    v = m.get("value")
    if isinstance(v, float) and not math.isfinite(v): return e + ["non-finite value"]
    if isinstance(v, float): e.append("float value forbidden (use integer or decimal string)")
    if isinstance(v, bool): return e + ["boolean value"]
    try: d = Decimal(str(v))
    except Exception: return e + ["unparseable value"]
    if not d.is_finite(): return e + ["non-finite value"]  # early return: no comparisons on NaN/Infinity
    if d.is_zero(): d = Decimal(0)  # '-0', '-0.0' are defined as exact zero (sign dropped)
    if "range" in spec and not (spec["range"][0] <= d <= spec["range"][1]): e.append(f"{m['metric']} outside {spec['range']}")
    if m["metric"] not in catalog["non_negative_metrics_except"] and d < 0: e.append("negative value")
    if m["unit"] in ("token", "request") and d != d.to_integral_value(): e.append("fractional token/request count")
    return e

def check_event(ev):
    e = []
    ps, pe = ev.get("period_start"), ev.get("period_end")
    if (ps is None) != (pe is None): e.append("period_start/period_end must both be set or both null")
    elif ps and _utc(ps) >= _utc(pe): e.append("period_start must precede period_end")
    w = ev.get("window")
    if w and _utc(w["starts_at"]) >= _utc(w["ends_at"]): e.append("window start must precede end")
    return e

def _utc(ts):
    from datetime import datetime, timezone
    s = ts.replace("Z", "+00:00")
    if "." in s:  # trim to microseconds for fromisoformat
        a, b = s.split(".", 1); frac = ""
        i = 0
        while i < len(b) and b[i].isdigit(): i += 1
        s = a + "." + b[:i][:6].ljust(6, "0") + b[i:]
    return datetime.fromisoformat(s).astimezone(timezone.utc)

def check_revision_chain(revs):
    """revs: list of revision objects of ONE logical measurement."""
    e, by = [], {r["revision_id"]: r for r in revs}
    if len(by) != len(revs): e.append("duplicate revision_id")
    for r in revs:
        s, seen = r["supersedes"], {r["revision_id"]}
        while s is not None:
            if s in seen: e.append(f"cycle at {r['revision_id']}"); break
            seen.add(s)
            if s not in by: e.append(f"dangling supersedes {s}"); break
            s = by[s]["supersedes"]
    eff = [r for r in revs if r["effective"]]
    if len(eff) != 1: e.append(f"exactly one effective revision required, got {len(eff)}")
    elif any(r["supersedes"] == eff[0]["revision_id"] for r in revs): e.append("effective revision is superseded")
    return e

def check_bindings(measurements, evidences):
    """Every measurement evidence_id must resolve to a descriptor; source/account must match (no cross-account proof)."""
    e, ev = [], {x["evidence_id"]: x for x in evidences}
    for m in measurements:
        for i in m["evidence_ids"]:
            x = ev.get(i)
            if x is None: e.append(f"unresolved evidence {i}"); continue
            if (x["source"], x["account"]) != (m["source"], m["source_account_id"]): e.append(f"evidence {i} scope mismatch")
    return e


import hashlib

def derive_idempotency_key(source, account, native_id, window):
    """Reference derivation (DQ03). native_id: provider/session/forge id or the documented source-specific key.
    window: dict(type, starts_at, ends_at) or None. Normalised to UTC so equal instants give equal keys.
    key = sha256(canonical JSON of [source, account, native_id, window_type, start_utc, end_utc])."""
    w = [None, None, None] if window is None else [window["type"], _utc(window["starts_at"]).isoformat(), _utc(window["ends_at"]).isoformat()]
    data = json.dumps(["ael.idem.v0.1", source, account, native_id] + w, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()

def verify_artifact(data: bytes, descriptor):
    """US-03.c / DQ02: stored bytes must match descriptor sha256 and size. Returns errors; a non-empty result means
    the evidence is 'not verified' (integrity violation) and derived reports must flag it."""
    e = []
    if hashlib.sha256(data).hexdigest() != descriptor["sha256"]: e.append("integrity violation: sha256 mismatch (evidence not verified)")
    if len(data) != descriptor["size_bytes"]: e.append("integrity violation: size mismatch (evidence not verified)")
    return e
