# Generic Mapping Importer — AEL Module Contract

## Purpose and Scope
`importers/mapping` implements the generic mapping importer for tabular/structured data (CSV, JSON, JSONL) into canonical AEL measurements and events according to `schema/core` (US-05, US-06).

## Key Capabilities
1. **Declared Mapping Only**: No guessed columns, no inferred schema fields. Every target metric, dimension, or timestamp binding must be explicitly specified in the mapping configuration.
2. **Explicit Format Controls (CSV/JSON/JSONL)**:
   - Delimiter (e.g. `,`, `;`, `\t`)
   - Decimal separator (e.g. `.`, `,`)
   - Timezone (explicit IANA or fixed offset, e.g. `UTC`, `+02:00`, `America/New_York`)
   - Quoting rules and comment character
3. **Strict Types & Null Disambiguation (R06)**:
   - Disambiguation between:
     - Missing / omitted key (`UNSET`)
     - Explicit empty string (`""`)
     - Explicit null / `None` (`EXPLICIT_NULL`)
     - Explicit zero (`0`, `0.0`, `"0"`, `"-0"`)
   - Rejection of invalid types without silent fallback.
   - Rejection of unsupported nesting: nested objects/lists without explicit leaf extractors are flagged as errors rather than flattened arbitrarily.
4. **Quarantine & Error Isolation (US-05.c/d)**:
   - Non-strict mode (`strict=False`): invalid records are routed to `quarantine` with reasons; import run status is `partial`.
   - Strict mode (`strict=True`): any validation or conversion error aborts record ingestion with zero accepted records; run status is `failed`.
   - Disjoint outcome partition:
     $$\text{total\_records} = \text{accepted} + \text{existing} + \text{quarantined} + \text{corrected}$$
5. **Idempotency & Replay (US-06.a, T01)**:
   - Deterministic event IDs / idempotency keys derived via `schema.core.semantic.derive_idempotency_key` or canonical record hashing.
   - Second import of the identical dataset detects `existing` records without modifying totals or active counts.
6. **Preview Mode (US-05.a)**:
   - Evaluates mapping over a subset of rows (or entire input) without emitting permanent store mutations, returning mapped previews, quarantine warnings, and header validations.
