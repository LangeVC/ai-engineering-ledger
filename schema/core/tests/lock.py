import hashlib, json, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = ["common.schema.json", "event.schema.json", "evidence.schema.json", "measurement.schema.json", "revision.schema.json", "units.catalog.json"]
def canon(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f: d = json.load(f)
    return hashlib.sha256(json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
def compute():
    files = {n: canon(n) for n in FILES}
    agg = hashlib.sha256("".join(f"{n}:{files[n]}\n" for n in sorted(files)).encode()).hexdigest()
    return {"schema_version": "0.1", "hash_algorithm": "sha256-canonical-json", "files": files, "aggregate": agg}
if __name__ == "__main__":
    p = os.path.join(ROOT, "schema.lock.json"); cur = compute()
    if "--write" in sys.argv:
        open(p, "w").write(json.dumps(cur, indent=2, sort_keys=True) + "\n"); print(cur["aggregate"])
    else:
        ok = json.load(open(p)) == cur; print("lock ok" if ok else "LOCK MISMATCH", cur["aggregate"]); sys.exit(0 if ok else 1)
