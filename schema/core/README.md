# schema/core — AEL contract v0.1

JSON Schema draft 2020-12. Collector- and export schemas live elsewhere and must not be defined here.

- `event.schema.json` envelope, window identity, idempotency key (US-03, DQ03)
- `measurement.schema.json` + `units.catalog.json` units/aggregation (US-03.a/b, DQ01/DQ04)
- `evidence.schema.json` capture point, redaction version, sha256 binding (US-03.c, PRD 10.3)
- `revision.schema.json` correction chain + lineage (US-04)
- `semantic.py` cross-document rules JSON Schema can't express (catalog, percent range, finite values, cycles, evidence resolution, single effective revision)
- `semantic.verify_artifact` (DQ02, US-03.c): stored bytes must match descriptor sha256/size, else "integrity violation / evidence not verified"; fixtures `fixtures/artifacts/*` (real artifact + one-byte-flipped copy)
- `semantic.derive_idempotency_key` (DQ03): sha256 over canonical JSON of [`ael.idem.v0.1`, source, account, native_id, window type, window start/end in UTC]; fixture vector pins the expected hash
- Numeric edge cases: non-finite values return early as rejected; `-0`/`-0.0` are defined as exact zero (accepted for percent)
- `tests/run_tests.py` also runs an optional `jsonschema` consistency check (skipped with a notice if not importable)
- `schema.lock.json` canonical hashes; `python3 tests/lock.py --write|--check`

## Compatibility rule (fixed for 0.1)
1. `schema_version` is `MAJOR.MINOR`. Within 0.x: adding optional properties or enum values is MINOR-compatible for readers; removing/renaming fields, tightening types/patterns, making a field required, or changing unit/metric semantics is breaking and requires a new MAJOR (or 0.MINOR bump with migration note while pre-1.0).
2. Any byte change to a schema changes the canonical hash in `schema.lock.json`; the lock must be updated deliberately in the same change that bumps version.
3. Canonical hash = SHA-256 of JSON re-serialised with sorted keys, separators `(",",":")`, `ensure_ascii=False`, UTF-8; aggregate hash = SHA-256 of `name:hash\n` lines sorted by file name.
4. Readers reject unknown `schema_version`; `additionalProperties:false` makes unknown fields errors.

Tests: `python3 schema/core/tests/run_tests.py` (own minimal 2020-12 subset validator, as `jsonschema` is not installed).
