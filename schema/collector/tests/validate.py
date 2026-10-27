#!/usr/bin/env python3
"""Validate collector fixtures. Uses jsonschema (draft 2020-12) if installed,
else a minimal built-in validator covering the keywords used by these schemas.
Plus semantic checks (profile binding, isolation, status aggregation, coverage nesting)."""
import json, os, re, sys, glob

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCHEMAS = {}
for p in glob.glob(os.path.join(ROOT, "*.schema.json")):
    s = json.load(open(p)); SCHEMAS[s["$id"]] = s
BASE = "https://ael.local/schema/collector/"


def resolve(ref, cur_id, root_schema):
    url, _, frag = ref.partition("#")
    doc_id = (BASE + url) if url else cur_id
    doc = SCHEMAS[doc_id]
    node = doc
    for part in [x for x in frag.split("/") if x]:
        node = node[part]
    return node, doc_id


def typ_ok(v, t):
    if isinstance(t, list):
        return any(typ_ok(v, x) for x in t)
    return {"object": isinstance(v, dict), "array": isinstance(v, list), "string": isinstance(v, str),
            "boolean": isinstance(v, bool), "null": v is None,
            "integer": isinstance(v, int) and not isinstance(v, bool),
            "number": isinstance(v, (int, float)) and not isinstance(v, bool)}[t]


def val(v, s, doc_id):
    """return list of error strings"""
    e = []
    if "$ref" in s:
        n, d = resolve(s["$ref"], doc_id, None)
        e += val(v, n, d)
    if "type" in s and not typ_ok(v, s["type"]): return e + ["type %s" % s["type"]]
    if "const" in s and not (v == s["const"] and type(v) == type(s["const"])): e.append("const")
    if "enum" in s and v not in s["enum"]: e.append("enum")
    if isinstance(v, str):
        if "pattern" in s and not re.search(s["pattern"].replace("(?i)", ""), v, re.I if s["pattern"].startswith("(?i)") else 0): e.append("pattern")
        if "minLength" in s and len(v) < s["minLength"]: e.append("minLength")
    if isinstance(v, (int, float)) and not isinstance(v, bool) and "minimum" in s and v < s["minimum"]: e.append("minimum")
    if isinstance(v, list):
        if "minItems" in s and len(v) < s["minItems"]: e.append("minItems")
        if "items" in s:
            for i, x in enumerate(v): e += ["[%d]%s" % (i, m) for m in val(x, s["items"], doc_id)]
    if isinstance(v, dict):
        for r in s.get("required", []):
            if r not in v: e.append("required " + r)
        props = s.get("properties", {})
        for k, x in v.items():
            if k in props: e += [k + "." + m for m in val(x, props[k], doc_id)]
            elif s.get("additionalProperties") is False: e.append("additional " + k)
    for sub in s.get("allOf", []): e += val(v, sub, doc_id)
    if "anyOf" in s and not any(not val(v, x, doc_id) for x in s["anyOf"]): e.append("anyOf")
    if "oneOf" in s and sum(1 for x in s["oneOf"] if not val(v, x, doc_id)) != 1: e.append("oneOf")
    if "not" in s and not val(v, s["not"], doc_id): e.append("not")
    if "if" in s:
        if not val(v, s["if"], doc_id):
            if "then" in s: e += val(v, s["then"], doc_id)
        elif "else" in s: e += val(v, s["else"], doc_id)
    return e


def schema_errors(inst, kind):
    sid = BASE + kind + ".schema.json"
    if HAVE_JS:
        import jsonschema
        from referencing import Registry, Resource
        reg = Registry().with_resources([(k, Resource.from_contents(v)) for k, v in SCHEMAS.items()])
        v = jsonschema.Draft202012Validator(SCHEMAS[sid], registry=reg)
        return [x.message for x in v.iter_errors(inst)]
    return val(inst, SCHEMAS[sid], sid)


def ts(x):
    from datetime import datetime
    return datetime.fromisoformat(x.replace("Z", "+00:00"))


def inside(a, b):
    return a and b and b["start"] <= a["start"] and a["end"] <= b["end"]


def semantic_errors(kind, d):
    e = []
    if kind == "account":
        for c in d.get("capabilities", []):
            if c["account_alias"] != d["alias"]: e.append("capability account mismatch (isolation)")
            if c["profile_binding"]["profile_id"] != d["profile_id"]: e.append("capability profile mismatch")
            if c["source"] != d["source"]: e.append("capability source mismatch")
            if c["product_domain"] not in d["product_domains"]: e.append("capability domain not declared")
    if kind == "run":
        keys = set()
        for a in d["account_runs"]:
            k = (a["source"], a["account_alias"], a["product_domain"], a["capability"])
            if k in keys: e.append("duplicate account_run key")
            keys.add(k)
            if "process" in a and a["process"]["profile_id"] != a["profile_id"]: e.append("process profile != account profile")
            c = a["coverage"]
            if c["delivered"] and not inside(c["delivered"], c["requested"]): e.append("delivered outside requested")
            if c["confirmed"] and not inside(c["confirmed"], c["delivered"]): e.append("confirmed outside delivered")
            if a["status"] == "success" and a["errors"]: e.append("success with errors")
            pr = a.get("process")
            if pr:
                if a["status"] == "success" and pr["exit_code"] != 0: e.append("success requires process.exit_code == 0")
                if a["status"] == "success" and (pr["stdout_truncated"] or pr["stderr_truncated"]): e.append("success requires untruncated stdout/stderr")
                if ts(pr["finished_at"]) < ts(pr["started_at"]): e.append("process finished_at < started_at")
        if ts(d["finished_at"]) < ts(d["started_at"]): e.append("run finished_at < started_at")
        sts = [a["status"] == "success" for a in d["account_runs"]]
        exp = "success" if all(sts) else ("failure" if not any(sts) else "partial_failure")
        if d["status"] != exp: e.append("run status %s != aggregate %s" % (d["status"], exp))
        # skipped_* count as non-success for aggregation
    return e


try:
    import jsonschema, referencing  # noqa
    HAVE_JS = True
except Exception:
    HAVE_JS = False


def main():
    print("validator:", "jsonschema" if HAVE_JS else "builtin-minimal")
    for p in SCHEMAS.values(): pass
    bad = 0; n = 0
    for sub, want_ok in (("valid", True), ("invalid", False)):
        for f in sorted(glob.glob(os.path.join(ROOT, "fixtures", sub, "*.json"))):
            kind = os.path.basename(f).split("-")[0]
            d = json.load(open(f))
            errs = schema_errors(d, kind)
            if not errs: errs = semantic_errors(kind, d)
            ok = not errs; n += 1
            res = "OK " if ok == want_ok else "FAIL"
            if ok != want_ok: bad += 1
            print(res, sub, os.path.basename(f), "" if ok else "-> " + "; ".join(errs[:2]))
    print("fixtures:", n, "failures:", bad)
    return 1 if bad else 0

if __name__ == "__main__":
    sys.exit(main())
