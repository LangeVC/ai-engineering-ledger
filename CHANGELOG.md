# Changelog

All notable changes to AI Engineering Ledger are documented here. Release
versions follow Semantic Versioning.

## 0.3.0 — 2026-10-30

### Study-data adapters and evidence contracts

- Added local Claude and Codex harness adapters with checkpointed,
  revision-aware deduplication, including stable replay of multiple versions of
  one native request.
- Added DeepSeek, five-account BytePlus, Kilo Code and faigate adapter paths,
  manual snapshot import, deterministic cost records, local Git metrics and
  Forgejo inventory evidence.
- Froze the initial study scope to `[2026-04-26, 2026-11-01)` in
  `Europe/Berlin`, with an explicit analysis cutoff and
  retrospective/prospective collection boundary.

### Canonical local analysis

- Added deterministic Analysis, Claims, Correlations and Study JSON contracts
  with explicit formula, unit, scope, evidence, coverage, confidence and
  unknown/partial states.
- Added one shared half-open filter and canonical calculation path for the
  local dashboard and JSON export, including DST-safe date boundaries and
  non-causal correlation guards.
- Added optional operator-triggered dashboard snapshot specifications bound to
  the active filter, dataset and selected claims.

### Privacy and reproducibility

- Added artifact-scoped privacy scanning for secrets, prompts, responses,
  email addresses, private paths/remotes, raw evidence and unapproved aliases.
- Added deterministic evidence-pack replay, independent SQL cross-checks for
  headline totals and consistent source-gap warnings across manifests,
  dashboard notices and Study JSON.
- Expanded local GUI source editing, bounded study views and regression
  coverage without adding any public publishing, article, social or SEO
  generation.

### Known limitations

- Subscription-plan telemetry is evidence-driven and may be partial or manual;
  raw token activity never asserts provider billing or remaining allowance.
- No output is attributed causally to a provider from temporal co-occurrence.
- The v0.3 dashboard and snapshots remain local; public study packaging and
  hosting are out of scope.
- SQLite is the only supported operational database in v0.3.0, and live
  provider availability still depends on the operator's account, plan and
  local credentials.

## 0.2.0 — 2026-10-28

### Local browser GUI

- Added the loopback-only `ael ui` evidence console with one-time bootstrap,
  session/CSRF protection and embedded offline assets.
- Added zero-write structured import previews, explicit hash-bound confirmation,
  secret-free source aliases and provider-report parity.
- Added private dashboard screenshot drafts with optional local Tesseract OCR;
  OCR values remain unclassified until human confirmation.
- Added browser security, upload lifecycle, accessibility, packaging and
  installed-wheel regression tests.

## 0.1.0 — 2026-10-28

The first public alpha establishes a local, evidence-first ledger for comparing
AI-provider usage, subscription quota, costs and engineering outcomes.

### Local operational ledger

- Added the SQLite 0.2 operational schema with migrations, revisions,
  idempotency keys and evidence references.
- Added deterministic import, collection, reporting, export, backup and restore
  command contracts.
- Preserved gauge, interval, delta and reset semantics instead of flattening
  incompatible provider measurements into one total.

### Provider and engineering sources

- Added capture and import contracts for DeepSeek, five isolated BytePlus
  Coding Plan accounts, Claude Pro, Codex Plus, Google AI Pro/Antigravity,
  Kilo Code and faigate.
- Added GitHub and Forgejo activity collector contracts.
- Added manual snapshot templates for subscription dashboards that do not
  expose a supported usage API.
- Added explicit supported, unavailable, unauthorized, unknown and unverified
  capability states.

### Provenance, quality and privacy

- Added evidence hashing, coverage and confidence metadata, revisions and
  deduplication rules.
- Added account isolation, decimal money handling, CSV injection protection and
  secret-redaction boundaries.
- Added protected local workspace creation with `0700` directories, a `0600`
  database, idempotent setup and symlink-path rejection.
- Excluded local databases, evidence, inboxes, exports, backups and SkillWeave
  execution state from the public repository.

### Distribution and governance

- Added the `ael` CLI package and Homebrew Formula template.
- Added private-Forgejo canonical and public-GitHub mirror declarations.
- Added ops-engine/lvc-ops release and mirror-health integration.
- Added public README, security policy, contribution guidance and Apache-2.0
  licensing.

### Known alpha limitations

- Subscription dashboard capture is manual until the local browser GUI ships.
- Provider availability varies by account, plan and region and is never assumed.
- Postgres remains a compatible future deployment target; SQLite is the only
  supported operational deployment in 0.1.0.
