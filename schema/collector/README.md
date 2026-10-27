# Collector contract (schema/collector) v0.1

JSON Schema (draft 2020-12) for capability, error, observation, account and collection run, plus fixtures and `tests/validate.py`.

## Consumers MUST run the semantic validator

The JSON Schemas alone are NOT sufficient. Cross-field/cross-object rules are enforced only by `semantic_errors` in `tests/validate.py` (run after schema validation). Consumers must execute it (or an equivalent port):

- account: capability alias/profile/source/domain match the account (isolation, profile binding)
- run: unique (source, account, domain, capability); process.profile_id == account_run.profile_id
- run: delivered within requested, confirmed within delivered
- run: aggregate status matches account runs; success has no errors
- run: success requires process.exit_code == 0 and untruncated stdout/stderr
- run/process: finished_at >= started_at (timestamps compared as UTC instants)

## Exit status (bindend gemäß ADR-0002)

`exit_status` 0 = success, 10 = partial_failure, 20 = failure. This enum is binding per ADR-0002 (0: success, 3: partial, 1: failure, 2: usage).

## Tests

`python3 schema/collector/tests/validate.py` (uses `jsonschema` if installed, else a built-in minimal validator).

## Konsumentenpflicht (Semantische Validierung)
Konsumenten (Collector-Adapter, Orchestrierung) MÜSSEN neben dem JSON-Schema zwingend die semantische Validierung (semantic_errors / tests/validate.py) ausführen. Insbesondere:
- success erfordert Kindprozess exit_code == 0 und keine Truncation
- finished_at >= started_at (mit Zeitzonen-Normalisierung)
- Truncation- und Pagination-Konsistenz
