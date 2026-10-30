# AI Engineering Ledger

[![Release](https://img.shields.io/github/v/release/LangeVC/ai-engineering-ledger?display_name=tag)](https://github.com/LangeVC/ai-engineering-ledger/releases)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](./pyproject.toml)
[![Status](https://img.shields.io/badge/status-alpha-orange.svg)](#project-status)

> [!NOTE]
> **Public mirror.** The canonical repository is the private Forgejo project at
> `git.langevc.com/langevc/ai-engineering-ledger`. GitHub is the public,
> read-only release and distribution mirror. Issues are welcome on GitHub;
> accepted changes are applied upstream and mirrored back.

A local-first ledger for AI-provider usage, cost, quota, provenance and the
engineering activity those resources produced.

Provider billing answers **what was consumed or charged**. Agent and gateway
telemetry explains **how and where it was consumed**. Git activity records
**what was produced**. AEL preserves these as separate evidence layers instead
of pretending that one source can replace the others.

## Why AEL

AI engineering work is increasingly spread across API credits, subscription
plans, coding agents, gateways and multiple Git forges. Their dashboards expose
different windows, units and reset semantics, and some provide no export at
all. Spreadsheet totals quickly become unverifiable.

AEL provides one local SQLite ledger with:

- evidence hashes and provenance for every imported observation;
- account isolation, including multiple accounts at the same provider;
- explicit gauge, interval, delta and reset semantics;
- idempotent historical backfills and continuing collection;
- manual dashboard snapshots when no supported API exists;
- deterministic provider/account reports without inventing missing values;
- privacy-first operation without storing prompts or chat content.

## Supported source families

| Source | Capture path |
|---|---|
| DeepSeek API | credit balance, response usage and historical CSV/JSON |
| BytePlus Coding Plan | isolated account snapshots, usage and billing imports |
| Claude Pro | manual quota/dashboard snapshots |
| Codex Plus | manual quota/dashboard snapshots |
| Google AI Pro / Antigravity | manual quota/dashboard snapshots |
| Kilo Code | session and aggregate usage imports |
| faigate | runtime and gateway telemetry import |
| GitHub | repository activity collector contract |
| Forgejo | repository activity collector contract |

Availability is reported as supported, unavailable, unauthorized, unknown or
unverified. Zero is recorded only when the evidence actually proves zero.

## Installation

### Browser GUI

Start the offline, loopback-only evidence console with:

```bash
ael ui
```

Use `ael ui --no-open --port 0` for automation. The GUI uses the same `AEL_HOME`, `AEL_DB_PATH`, SQLite ledger, provenance and deduplication contracts as the CLI. Structured imports are previewed without writes and require explicit confirmation.

Dashboard screenshots always create an untrusted review draft. If the local `tesseract` executable is available (`brew install tesseract`), AEL includes OCR text and unclassified numeric candidates; it never assigns quota or reset semantics automatically. Without Tesseract, manual review remains fully functional. No screenshot or OCR text is sent to a cloud service.

### Homebrew

```bash
brew tap langevc/tap
brew install ael
ael doctor
```

The Formula creates a protected workspace under Homebrew's `var/lib/ael`.
Upgrades and uninstall/reinstall cycles preserve the ledger and inbox.

### From a checkout

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
ael setup
ael doctor
```

## Quickstart

```bash
# Show resolved paths and initialise the protected workspace.
ael paths
ael setup

# Inspect available commands and adapter-specific inputs.
ael --help
ael import --help
ael collect --help
ael study --help

# Validate the installation and SQLite schema.
ael doctor
```

### Living-study workflow (v0.4)

```bash
# Show due configured jobs without contacting a provider.
ael collect run --dry-run

# Materialize an immutable build from the current ledger range.
ael study build --start 2026-04-26 --end-exclusive 2026-11-01
ael study list

# Compare two verified builds and export data-only JSON.
ael study diff --from-snapshot SNAPSHOT_A --to-snapshot SNAPSHOT_B --output diff.json
ael study export --snapshot latest --from-snapshot SNAPSHOT_A --output living-study.json
```

Finalized builds are content-addressed below `<AEL_HOME>/living-study/`.
Warnings are visible but do not act as a publication gate. Public hosting,
articles, social assets and SEO remain outside AEL.

By default, AEL uses:

- macOS: `~/Library/Application Support/ael`
- Linux: `${XDG_DATA_HOME:-~/.local/share}/ael`
- database: `<AEL_HOME>/ledger.db`

`AEL_HOME`, `AEL_DB_PATH` and explicit CLI paths override these defaults in
that order. Workspace directories are `0700`; the SQLite database is `0600`.

See the [local operations guide](docs/operations/LOCAL_USAGE_DE.md) for capture
templates, protected-inbox handling, imports and reporting.

## Data model and quality rules

AEL keeps raw evidence, normalized observations and derived values distinct.
Each measurement carries its source, account scope, capture time, coverage,
confidence and evidence references. Re-importing the same semantic observation
does not duplicate it. Corrections create revisions rather than rewriting
history silently.

Counter resets, rolling limits and late-arriving observations remain explicit.
Costs retain decimal values and currencies; subscription entitlements are not
silently converted into hypothetical API spend.

## Privacy and security

- Data stays local unless the operator deliberately exports it.
- Credentials remain in environment variables or existing provider CLI profiles.
- Prompt and chat content is not required.
- Inbox, evidence, exports, backups and SQLite files are ignored by Git.
- Setup rejects symlinked workspace paths and enforces private permissions.
- Public fixtures use synthetic accounts and values only.

Read [SECURITY.md](SECURITY.md) before reporting a vulnerability.

## Architecture

```text
provider APIs / CLIs / dashboard snapshots / git forges
                         |
                         v
              collectors and importers
                         |
                         v
          raw evidence + provenance + revisions
                         |
                         v
              local SQLite operational ledger
                         |
                         v
           deterministic reports and exports
```

SQLite is the default and first-class deployment. The contracts keep a future
Postgres deployment possible without making a server a prerequisite.

## Project status

Version 0.4 is the current alpha release. The CLI, schema, local workspace,
operational imports and reporting contracts are usable. It includes local
harness adapters, canonical filtered Study JSON, persistent collection jobs,
source health, immutable Study builds, semantic diffs, additive data-only JSON
and a local Living Study view while retaining v0.3 read compatibility.
Optional local Tesseract capture remains available. Provider capabilities
remain evidence-driven and may differ by account, plan and region.

## Development

```bash
python3 -m pytest -q
python3 tests/run_all.py
```

Contributions must preserve account isolation, provenance, idempotency and the
private-Forgejo/public-mirror boundary. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Apache-2.0. See [LICENSE](LICENSE).

---

> [!NOTE]
> The canonical source is maintained on private Forgejo. The GitHub repository
> is the public read-only mirror used for releases and installation. GitHub
> issues are welcome; accepted patches are landed upstream and synchronized.
