#!/usr/bin/env python3
"""Testharness-Wurzel (Eigentuemer: AEL-BOOTSTRAP). Fuehrt die freigegebenen Contract-Suiten
ueber Pfad aus (keine Kopie). Contract-Wurzel: $AEL_SCHEMA_DIR, sonst ./schema, sonst
aufwaerts gesuchtes <vorfahre>/schema/core. Mit --strict muss jsonschema importierbar sein."""
import os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__)); REPO = os.path.dirname(HERE)
SUITES = [("core", "run_tests.py", []), ("core-lock", "lock.py", ["--check"]),
          ("collector", "validate.py", []), ("export", "validate.py", [])]

def find_schema():
    c = [os.environ.get("AEL_SCHEMA_DIR"), os.path.join(REPO, "schema")]
    p = REPO
    while True:
        c.append(os.path.join(p, "schema"))
        n = os.path.dirname(p)
        if n == p: break
        p = n
    for x in c:
        if x and os.path.isdir(os.path.join(x, "core")) and os.path.isdir(os.path.join(x, "export")): return x
    sys.exit("contract schema dir not found (set AEL_SCHEMA_DIR)")

def module_skeleton():
    import json
    miss = []
    for m in json.load(open(os.path.join(HERE, "modules.json")))["modules"]:
        if not os.path.isdir(os.path.join(REPO, m)): miss.append(m)
    return miss

def main():
    strict = "--strict" in sys.argv
    if strict:
        try: import jsonschema, referencing  # noqa
        except ImportError: sys.exit("--strict: jsonschema/referencing missing (uv run --group dev ...)")
    sd = find_schema(); fails = 0
    print("schema dir:", sd)
    for name, script, args in SUITES:
        suite = "core" if name.startswith("core") else name
        r = subprocess.run([sys.executable, os.path.join(sd, suite, "tests", script), *args], cwd=sd)
        print(f"[{name}] exit {r.returncode}"); fails += r.returncode != 0
    miss = module_skeleton()
    print("[modules] missing:", miss or "none"); fails += bool(miss)
    app = subprocess.run(
        [sys.executable, "-m", "unittest", "-v", "tests.test_remaining_stories"],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": REPO + os.pathsep + os.environ.get("PYTHONPATH", "")},
    )
    print("[application] exit", app.returncode); fails += app.returncode != 0
    print("RESULT", "FAIL" if fails else "OK"); return 1 if fails else 0

if __name__ == "__main__": sys.exit(main())
