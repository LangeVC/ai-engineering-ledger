# File-Based Import Pipeline — AEL Module Contract (US-05)

## Purpose and Scope
`importers/files` implements the file-based historical import pipeline for AI Engineering Ledger (AEL v0.1).
It consumes structured historical exports (CSV, JSON, JSONL) from LLM providers (e.g. DeepSeek) or generic tabular sources by orchestrating `importers/mapping` and provider adapters (such as `collectors/deepseek`).

## Acceptance Criteria Conformance (PRD US-05)

### US-05.a: Multi-Format Import & Format Negotiation
- **Documented Import Paths**: CSV, JSON, and JSONL formats are natively parsed and supported.
- **Preview Capability**: `FileImporter.preview(...)` inspects raw text, detects headers, provides sample rows without persistent mutations, and details explicit format options.
- **Explicit CSV Controls**: Delimiters (`,`, `;`, `\t`, `|`), decimal separators (`.`, `,`), and timezones (IANA e.g. `Europe/Berlin`, `UTC`, or fixed offsets `+02:00`) are fully configurable and respected.
- **Unsupported Nesting Explanation**: When JSON/JSONL rows contain nested structures (lists/dicts) without explicit column mapping, the pipeline explains why the structure cannot be mapped automatically instead of guessing or silent truncation.

### US-05.b: Real Minimized DeepSeek Fixture & Exact Sum Reconciliation
- Real minimized DeepSeek export fixture (`deepseek-sample-export.csv`) with verified token semantics:
  - Input tokens (`prompt_tokens`)
  - Cache hit tokens (`prompt_cache_hit_tokens`)
  - Cache miss tokens (`prompt_cache_miss_tokens`)
  - Output tokens (`completion_tokens`)
  - Reasoning tokens (`reasoning_tokens`)
- Exact sum reconciliations: Group totals by `(account, date/period, model, currency)` match independently calculated sums with zero loss of decimal precision (`Decimal` cost calculation).
- DeepSeek token invariants enforced:
  - `prompt_tokens == prompt_cache_hit_tokens + prompt_cache_miss_tokens`
  - `prompt_cache_hit_tokens >= 0`
  - `prompt_cache_miss_tokens >= 0`
  - `completion_tokens >= 0`
  - `reasoning_tokens <= completion_tokens`

### US-05.c: Quarantine Accounting & Disjoint Outcome Partition
Every import report partitions total read records into four mutually disjoint categories:
$$\text{total\_read} = \text{accepted} + \text{existing} + \text{quarantined} + \text{corrected}$$
- `accepted`: Fresh records successfully parsed, validated, and converted into canonical events/measurements.
- `existing`: Duplicate records recognized via canonical idempotency key hashing (no mutation or duplicate count).
- `quarantined`: Faulty records isolated due to parse errors or invariant breaches.
- `corrected`: Records replacing prior revisions or applying verified adjustments.
- In strict mode (`strict_mode=True`), any quarantine record marks the run as `failed` and rejects ingestion.
- In non-strict mode, invalid records result in a `partial` status run, never falsely reporting `complete`.

### US-05.d: Error Isolation, Partial Imports & Detailed Quarantine Reporting
- **Fault Isolation with Valid Record Retention**: Faulty records are isolated into quarantine without discarding valid records in the same run (when `strict_mode=False`).
- **Partial Run Classification**: Any run containing both valid and quarantined records is tagged `partial` and never `complete`.
- **Strict Mode Enforcement**: When `strict_mode=True`, any validation failure rejects all measurements, setting accepted to 0 and status to `failed`.
- **Detailed Quarantine Error Reports**: Each isolated quarantine record captures:
  - `line_number`: 1-based index or row reference within the source file.
  - `field`: Specific column or schema field triggering the failure (e.g. `prompt_tokens`, `completion_tokens`, `date`, `model`, `cost`).
  - `cause`: Root cause explanation detailing the invariant violation or parse failure.
  - `raw_error`: Original validator/parser message.
  - `source_record`: Original unmutated input row payload for auditing and replay.
