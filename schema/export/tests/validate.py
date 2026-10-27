#!/usr/bin/env python3
"""Validates schema/export fixtures. Uses jsonschema if installed, else a minimal
draft-2020-12 subset checker (type, const, enum, required, properties,
additionalProperties, items, minItems, minLength, minimum, pattern, local $ref)."""
import csv, glob, json, os, re, sys
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
def load(p): return json.load(open(os.path.join(ROOT, p), encoding="utf-8"))
TYPES = {"object": dict, "array": list, "string": str, "boolean": bool}
def check(v, s, root, path="$"):
    e = []
    if "$ref" in s:
        node = root
        for part in s["$ref"][2:].split("/"): node = node[part]
        return check(v, node, root, path)
    t = s.get("type")
    if t == "integer":
        if not (isinstance(v, int) and not isinstance(v, bool)): return [f"{path}: not integer"]
    elif t and not isinstance(v, TYPES[t]): return [f"{path}: not {t}"]
    if "const" in s and v != s["const"]: e.append(f"{path}: const")
    if "enum" in s and v not in s["enum"]: e.append(f"{path}: enum")
    if isinstance(v, str):
        if "pattern" in s and not re.search(s["pattern"], v): e.append(f"{path}: pattern")
        if len(v) < s.get("minLength", 0): e.append(f"{path}: minLength")
    if isinstance(v, int) and not isinstance(v, bool) and v < s.get("minimum", v): e.append(f"{path}: minimum")
    if isinstance(v, dict):
        for r in s.get("required", []):
            if r not in v: e.append(f"{path}: missing {r}")
        props = s.get("properties", {}); ap = s.get("additionalProperties", True)
        for k, x in v.items():
            if k in props: e += check(x, props[k], root, f"{path}.{k}")
            elif ap is False: e.append(f"{path}: extra {k}")
            elif isinstance(ap, dict): e += check(x, ap, root, f"{path}.{k}")
    if isinstance(v, list):
        if len(v) < s.get("minItems", 0): e.append(f"{path}: minItems")
        if "items" in s:
            for i, x in enumerate(v): e += check(x, s["items"], root, f"{path}[{i}]")
    return e
def validate(v, s):
    try:
        import jsonschema
        jsonschema.Draft202012Validator.check_schema(s)
        return [str(x.message) for x in jsonschema.Draft202012Validator(s).iter_errors(v)]
    except ImportError:
        return check(v, s, s)
EXIT = {"success": 0, "partial": 3, "failure": 1}
def cli_semantics(d):
    e = []
    if EXIT.get(d.get("status")) != d.get("exit_code"): e.append("status/exit_code mismatch")
    st = [r.get("status") for r in d.get("runs", [])]
    if d.get("status") == "success" and "failed" in st: e.append("success with failed run")
    if d.get("status") == "failure" and "success" in st: e.append("failure with successful run")
    if d.get("status") == "partial" and not ("failed" in st and "success" in st): e.append("partial needs success+failed")
    return e
TRIG = "=+-@\t\r\n '"
def csv_guard(x):
    """CSV-only neutralization of text cells; JSON never uses this."""
    return "'" + x if x and x[0] in TRIG else x
def csv_unguard(x): return x[1:] if x.startswith("'") else x
CELL = {"integer": r"^-?\d+$", "decimal": r"^-?\d+(\.\d+)?$",
        "timestamp": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$",
        "date": r"^\d{4}-\d{2}-\d{2}$", "offset": r"^[+-]\d{2}:\d{2}$"}
def csv_errors(path, cols):
    rows = list(csv.reader(open(path, encoding="utf-8", newline="")))
    names = [c[0] for c in cols]; e = []
    if rows[0] != names: return ["header"]
    for n, r in enumerate(rows[1:], 1):
        if len(r) != len(cols): e.append(f"row{n} width"); continue
        for c, v in zip(cols, r):
            if c[1] == "text":
                if v and v[0] not in "'" and v[0] in TRIG: e.append(f"row{n} {c[0]} unguarded")
            elif c[1] in CELL:
                if not re.match(CELL[c[1]], v): e.append(f"row{n} {c[0]} bad {c[1]}")
            else: e.append(f"unknown type {c[1]}")
    return e
def main():
    fails = 0
    def ok(c, name):
        nonlocal fails
        print(("PASS " if c else "FAIL ") + name); fails += (not c)
    cli, man = load("cli-result.schema.json"), load("manifest.schema.json")
    for f in sorted(glob.glob(ROOT + "/fixtures/valid/cli-*.json")):
        d = json.load(open(f)); ok(not validate(d, cli) and not cli_semantics(d), "valid " + os.path.basename(f))
    for f in sorted(glob.glob(ROOT + "/fixtures/invalid/cli-*.json")):
        d = json.load(open(f)); ok(bool(validate(d, cli) or cli_semantics(d)), "rejected " + os.path.basename(f))
    d = json.load(open(ROOT + "/fixtures/valid/manifest.json"))
    ok(not validate(d, man), "valid manifest.json")
    ok(sorted(t["name"] for t in d["tables"]) == sorted(load("columns.json")["tables"]), "manifest tables == column catalog")
    for f in sorted(glob.glob(ROOT + "/fixtures/invalid/manifest-*.json")):
        ok(bool(validate(json.load(open(f)), man)), "rejected " + os.path.basename(f))
    cols = load("columns.json")["tables"]["dataset"]
    ok(not csv_errors(ROOT + "/fixtures/valid/dataset.csv", cols), "valid dataset.csv (typed cells + guarded text)")
    for f in sorted(glob.glob(ROOT + "/fixtures/invalid/dataset-*.csv")):
        ok(bool(csv_errors(f, cols)), "rejected " + os.path.basename(f))
    rows = list(csv.DictReader(open(ROOT + "/fixtures/valid/dataset.csv", encoding="utf-8", newline="")))
    ok(any(r["cost"].startswith("-") for r in rows) and any(r["observed_at_source_offset"].startswith("-") for r in rows), "negative numeric/offset cells unchanged")
    starts = {r["model"][1:2] for r in rows if r["model"].startswith("'")}
    ok(set("=+-@\t\r\n '") <= starts, "fixture covers every trigger char")
    for raw in ["=1+1", "+1", "-a", "@x", "\tx", "\rx", "\nx", " x", "'abc", "''", "plain", ""]:
        ok(csv_unguard(csv_guard(raw)) == raw, f"roundtrip {raw!r}")
    naive = lambda x: "'" + x if x and x[0] in "=+-@\t\r\n " else x
    ok(naive("'abc") == naive("abc")[:0] + "'abc" and csv_unguard(naive("'abc")) != "'abc", "negative: guard without ' trigger is NOT injective")
    ok(len({csv_guard(x) for x in ["'abc", "abc", "=a"]}) == 3, "guard injective sample")
    ok("Status: bindend gemäß ADR-0002" in open(ROOT + "/CONTRACT.md", encoding="utf-8").read(), "CONTRACT exit table marked binding per ADR-0002")
    for f in sorted(glob.glob(ROOT + "/fixtures/valid/manifest-tz-*.json")):
        ok(not validate(json.load(open(f)), man), "valid " + os.path.basename(f))
    raw = "=HYPERLINK(1)"; ok(json.loads(json.dumps({"v": raw}))["v"] == raw, "JSON preserves raw value")
    print("FAILURES:", fails); return 1 if fails else 0
if __name__ == "__main__": sys.exit(main())
