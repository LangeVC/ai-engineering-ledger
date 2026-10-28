# Changelog

All notable changes to AI Engineering Ledger are documented here. Release
versions follow Semantic Versioning.

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
