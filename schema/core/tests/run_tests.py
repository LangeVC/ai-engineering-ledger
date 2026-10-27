import json, os, sys
from decimal import Decimal
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT); sys.path.insert(0, HERE)
import minival, semantic, lock

L = minival.Loader(ROOT)
CAT = L.doc("units.catalog.json")
SCHEMA = {"event": "event.schema.json", "measurement": "measurement.schema.json", "evidence": "evidence.schema.json", "revision": "revision.schema.json"}

def problems(kind, doc):
    if kind in SCHEMA:
        e = minival.validate(doc, SCHEMA[kind], L)
        if not e and kind == "event": e += semantic.check_event(doc)
        if not e and kind == "measurement": e += semantic.check_measurement(doc, CAT)
        return e
    if kind == "chain": return semantic.check_revision_chain(doc)
    if kind == "bundle": return semantic.check_bindings(doc["measurements"], doc["evidence"])
    if kind == "idem":
        i, r = doc["inputs"], doc["replay_inputs"]
        k1 = semantic.derive_idempotency_key(i["source"], i["account"], i["native_id"], i["window"])
        k2 = semantic.derive_idempotency_key(r["source"], r["account"], r["native_id"], r["window"])
        return ([] if k1 == doc["expected"] else [f"derived key {k1} != expected vector"]) + ([] if k1 == k2 else ["replay key differs"])
    if kind == "artifact":
        return semantic.verify_artifact(open(os.path.join(ROOT, "fixtures", doc["artifact"]), "rb").read(), doc["descriptor"])
    if kind == "sum":
        s = sum(Decimal(x) for x in doc["parts"]); return [] if s == Decimal(doc["expected_sum"]) else [f"sum {s} != {doc['expected_sum']}"]
    raise SystemExit(f"unknown kind {kind}")

fail = 0
for c in json.load(open(os.path.join(ROOT, "fixtures", "manifest.json"))):
    text = open(os.path.join(ROOT, "fixtures", c["file"]), encoding="utf-8").read()
    try: doc = semantic.load_json_strict(text); errs = problems(c["kind"], doc)
    except ValueError as x: errs = [f"load: {x}"]
    ok = not errs
    st = "ok  " if ok == c["expect_valid"] else "FAIL"
    if ok != c["expect_valid"]: fail += 1
    print(f"{st} {c['file']:48s} valid={ok} {'; '.join(errs)[:90]}")
rc = 0 if lock.compute() == json.load(open(os.path.join(ROOT, "schema.lock.json"))) else 1 if os.path.exists(os.path.join(ROOT, "schema.lock.json")) else 2
print("lock", "ok" if rc == 0 else "MISSING/MISMATCH"); fail += (rc != 0)
# unit tests for check_measurement edge cases (fix 3)
def mm(v, metric="quota_percent_used", unit="percent"): return {"metric": metric, "unit": unit, "value": v}
for name, m, want in [
    ("NaN string -> rejected, no exception", mm("NaN"), True), ("Infinity string", mm("Infinity"), True),
    ("float nan", mm(float("nan")), True), ("float inf", mm(float("inf")), True),
    ("-0.0 percent is exact zero -> accepted", mm("-0.0"), False), ("-0 percent accepted", mm("-0"), False),
    ("-0.1 percent rejected", mm("-0.1"), True), ("100.0001 percent rejected", mm("100.0001"), True)]:
    try: bad = bool(semantic.check_measurement(m, CAT))
    except Exception as x: bad = None; print("EXC", x)
    ok = bad == want; fail += (not ok); print("ok  " if ok else "FAIL", "unit:", name)
# fix 4: optional jsonschema consistency
try:
    import jsonschema  # noqa
except ImportError:
    print("SKIP jsonschema consistency test: jsonschema not importable (install it to enable)")
else:
    from referencing import Registry, Resource
    reg = Registry()
    for n in ["common", "event", "evidence", "measurement", "revision"]:
        d = L.doc(n + ".schema.json"); reg = reg.with_resource(d["$id"], Resource.from_contents(d))
    for c in json.load(open(os.path.join(ROOT, "fixtures", "manifest.json"))):
        if c["kind"] not in SCHEMA: continue
        try: doc = semantic.load_json_strict(open(os.path.join(ROOT, "fixtures", c["file"])).read())
        except ValueError: continue
        v = jsonschema.Draft202012Validator(L.doc(SCHEMA[c["kind"]]), registry=reg)
        mine = not minival.validate(doc, SCHEMA[c["kind"]], L); ref = v.is_valid(doc)
        if mine != ref: fail += 1; print("FAIL jsonschema disagreement", c["file"], mine, ref)
    print("jsonschema consistency checked")
print("FAILURES:", fail); sys.exit(1 if fail else 0)
