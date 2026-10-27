# ADR-0004 Lane-Bindung und Schreibflächen
Status: Plan (keine Worktrees erzeugt; Pfade/Branches sind Planwerte) · Datum 2026-10-26

Reale Basis: `166a02dd5052c7e25770745b47d8aac366480307` (main, Initialcommit nur `.gitignore`, kein Remote, kein Push; Quelle `.skillweave/runtime/preflight/git-base.txt`). Alle Lanes starten von dieser Basis bzw. dem vom Integrator freigegebenen Stand; Contracts liegen ungetrackt im Hauptbaum `schema/` und werden per Pfad (`AEL_SCHEMA_DIR`) gelesen, nicht kopiert.

Existierende Worktrees laut Dateisystem (nicht von BOOTSTRAP erzeugt): ael-bootstrap, ael-contract-{event,collect,export}.

| Lane | Worktree | Branch | Schreibfläche | Abhängigkeiten |
|---|---|---|---|---|
| AEL-ADAPTER-DEEPSEEK | `.skillweave/worktrees/ael-adapter-deepseek` | `ael/ael-adapter-deepseek` | `collectors/deepseek` | AEL-BOOTSTRAP, AEL-CAP-PROVIDER |
| AEL-ADAPTER-FAIGATE | `.skillweave/worktrees/ael-adapter-faigate` | `ael/ael-adapter-faigate` | `collectors/faigate` | AEL-BOOTSTRAP, AEL-CAP-RUNTIME |
| AEL-ADAPTER-FORGEJO | `.skillweave/worktrees/ael-adapter-forgejo` | `ael/ael-adapter-forgejo` | `collectors/forgejo` | AEL-BOOTSTRAP, AEL-CAP-FORGE |
| AEL-ADAPTER-GENERIC | `.skillweave/worktrees/ael-adapter-generic` | `ael/ael-adapter-generic` | `importers/mapping` | AEL-BOOTSTRAP, AEL-CAP-PROVIDER |
| AEL-ADAPTER-GITHUB | `.skillweave/worktrees/ael-adapter-github` | `ael/ael-adapter-github` | `collectors/github` | AEL-BOOTSTRAP, AEL-CAP-FORGE |
| AEL-ADAPTER-KILO | `.skillweave/worktrees/ael-adapter-kilo` | `ael/ael-adapter-kilo` | `collectors/kilo` | AEL-BOOTSTRAP, AEL-CAP-RUNTIME |
| AEL-BOOTSTRAP | `.skillweave/worktrees/ael-bootstrap` | `ael/ael-bootstrap` | `cmd/ael` | AEL-CONTRACT-EVENT, AEL-CONTRACT-COLLECT, AEL-CONTRACT-EXPORT |
| AEL-CAP-FORGE | `.skillweave/worktrees/ael-cap-forge` | `ael/ael-cap-forge` | `.skillweave/evidence/forge-capabilities` | AEL-PREFLIGHT |
| AEL-CAP-PROVIDER | `.skillweave/worktrees/ael-cap-provider` | `ael/ael-cap-provider` | `.skillweave/evidence/provider-capabilities` | AEL-PREFLIGHT |
| AEL-CAP-RUNTIME | `.skillweave/worktrees/ael-cap-runtime` | `ael/ael-cap-runtime` | `.skillweave/evidence/runtime-capabilities` | AEL-PREFLIGHT |
| AEL-CONTRACT-COLLECT | `.skillweave/worktrees/ael-contract-collect` | `ael/ael-contract-collect` | `schema/collector` | AEL-PREFLIGHT |
| AEL-CONTRACT-EVENT | `.skillweave/worktrees/ael-contract-event` | `ael/ael-contract-event` | `schema/core` | AEL-PREFLIGHT |
| AEL-CONTRACT-EXPORT | `.skillweave/worktrees/ael-contract-export` | `ael/ael-contract-export` | `schema/export` | AEL-PREFLIGHT |
| AEL-INTEGRATE | `.skillweave/worktrees/ael-integrate` | `ael/ael-integrate` | `cmd/ael` | AEL-US01-02, AEL-US02-01, AEL-US03-02, AEL-US04-01, AEL-US05-02, AEL-US06-02, AEL-US07-02, AEL-US08-02, AEL-US09-02, AEL-US10-02, AEL-US12-02, AEL-US13-02, AEL-US11-01, AEL-US14-02, AEL-US15-02, AEL-US16-02, AEL-US17-02, AEL-US18-02, AEL-US19-02 |
| AEL-PILOT | `.skillweave/worktrees/ael-pilot` | `ael/ael-pilot` | `.skillweave/evidence/pilot` | AEL-INTEGRATE |
| AEL-PREFLIGHT | `.skillweave/worktrees/ael-preflight` | `ael/ael-preflight` | `.skillweave/runtime/preflight` | keine |
| AEL-US01-01 | `.skillweave/worktrees/ael-us01-01` | `ael/ael-us01-01` | `config/sources` | AEL-BOOTSTRAP |
| AEL-US01-02 | `.skillweave/worktrees/ael-us01-02` | `ael/ael-us01-02` | `config/sources` | AEL-US01-01 |
| AEL-US02-01 | `.skillweave/worktrees/ael-us02-01` | `ael/ael-us02-01` | `collectors/account` | AEL-US01-02, AEL-BOOTSTRAP |
| AEL-US03-01 | `.skillweave/worktrees/ael-us03-01` | `ael/ael-us03-01` | `storage/evidence` | AEL-US01-02, AEL-BOOTSTRAP |
| AEL-US03-02 | `.skillweave/worktrees/ael-us03-02` | `ael/ael-us03-02` | `storage/evidence` | AEL-US03-01 |
| AEL-US04-01 | `.skillweave/worktrees/ael-us04-01` | `ael/ael-us04-01` | `storage/revisions` | AEL-US03-02, AEL-BOOTSTRAP |
| AEL-US05-01 | `.skillweave/worktrees/ael-us05-01` | `ael/ael-us05-01` | `importers/files` | AEL-US03-02, AEL-BOOTSTRAP, AEL-CAP-PROVIDER, AEL-ADAPTER-DEEPSEEK, AEL-ADAPTER-GENERIC |
| AEL-US05-02 | `.skillweave/worktrees/ael-us05-02` | `ael/ael-us05-02` | `importers/files` | AEL-US05-01 |
| AEL-US06-01 | `.skillweave/worktrees/ael-us06-01` | `ael/ael-us06-01` | `collectors/backfill` | AEL-US05-02, AEL-BOOTSTRAP |
| AEL-US06-02 | `.skillweave/worktrees/ael-us06-02` | `ael/ael-us06-02` | `collectors/backfill` | AEL-US06-01 |
| AEL-US07-01 | `.skillweave/worktrees/ael-us07-01` | `ael/ael-us07-01` | `importers/manual` | AEL-US03-02, AEL-BOOTSTRAP, AEL-CAP-PROVIDER |
| AEL-US07-02 | `.skillweave/worktrees/ael-us07-02` | `ael/ael-us07-02` | `importers/manual` | AEL-US07-01 |
| AEL-US08-01 | `.skillweave/worktrees/ael-us08-01` | `ael/ael-us08-01` | `collectors/byteplus` | AEL-US02-01, AEL-US03-02, AEL-BOOTSTRAP, AEL-CAP-PROVIDER |
| AEL-US08-02 | `.skillweave/worktrees/ael-us08-02` | `ael/ael-us08-02` | `collectors/byteplus` | AEL-US08-01 |
| AEL-US09-01 | `.skillweave/worktrees/ael-us09-01` | `ael/ael-us09-01` | `collectors/orchestration` | AEL-US02-01, AEL-US03-02, AEL-BOOTSTRAP |
| AEL-US09-02 | `.skillweave/worktrees/ael-us09-02` | `ael/ael-us09-02` | `collectors/orchestration` | AEL-US09-01 |
| AEL-US10-01 | `.skillweave/worktrees/ael-us10-01` | `ael/ael-us10-01` | `collectors/runtime` | AEL-US03-02, AEL-BOOTSTRAP, AEL-CAP-RUNTIME, AEL-ADAPTER-KILO, AEL-ADAPTER-FAIGATE |
| AEL-US10-02 | `.skillweave/worktrees/ael-us10-02` | `ael/ael-us10-02` | `collectors/runtime` | AEL-US10-01 |
| AEL-US11-01 | `.skillweave/worktrees/ael-us11-01` | `ael/ael-us11-01` | `reconcile/correlation` | AEL-US10-02, AEL-US13-02, AEL-BOOTSTRAP |
| AEL-US12-01 | `.skillweave/worktrees/ael-us12-01` | `ael/ael-us12-01` | `collectors/forge` | AEL-US03-02, AEL-BOOTSTRAP, AEL-CAP-FORGE, AEL-ADAPTER-GITHUB, AEL-ADAPTER-FORGEJO |
| AEL-US12-02 | `.skillweave/worktrees/ael-us12-02` | `ael/ael-us12-02` | `collectors/forge` | AEL-US12-01 |
| AEL-US13-01 | `.skillweave/worktrees/ael-us13-01` | `ael/ael-us13-01` | `derive/engineering` | AEL-US12-02, AEL-BOOTSTRAP |
| AEL-US13-02 | `.skillweave/worktrees/ael-us13-02` | `ael/ael-us13-02` | `derive/engineering` | AEL-US13-01 |
| AEL-US14-01 | `.skillweave/worktrees/ael-us14-01` | `ael/ael-us14-01` | `derive/delta` | AEL-US04-01, AEL-BOOTSTRAP |
| AEL-US14-02 | `.skillweave/worktrees/ael-us14-02` | `ael/ael-us14-02` | `derive/delta` | AEL-US14-01 |
| AEL-US15-01 | `.skillweave/worktrees/ael-us15-01` | `ael/ael-us15-01` | `reconcile/coverage` | AEL-US06-02, AEL-US08-02, AEL-US10-02, AEL-US14-02, AEL-BOOTSTRAP |
| AEL-US15-02 | `.skillweave/worktrees/ael-us15-02` | `ael/ael-us15-02` | `reconcile/coverage` | AEL-US15-01 |
| AEL-US16-01 | `.skillweave/worktrees/ael-us16-01` | `ael/ael-us16-01` | `derive/cost` | AEL-US04-01, AEL-US05-02, AEL-BOOTSTRAP |
| AEL-US16-02 | `.skillweave/worktrees/ael-us16-02` | `ael/ael-us16-02` | `derive/cost` | AEL-US16-01 |
| AEL-US17-01 | `.skillweave/worktrees/ael-us17-01` | `ael/ael-us17-01` | `export/study` | AEL-US11-01, AEL-US13-02, AEL-US15-02, AEL-US16-02, AEL-BOOTSTRAP |
| AEL-US17-02 | `.skillweave/worktrees/ael-us17-02` | `ael/ael-us17-02` | `export/study` | AEL-US17-01 |
| AEL-US18-01 | `.skillweave/worktrees/ael-us18-01` | `ael/ael-us18-01` | `storage/recovery` | AEL-US04-01, AEL-BOOTSTRAP |
| AEL-US18-02 | `.skillweave/worktrees/ael-us18-02` | `ael/ael-us18-02` | `storage/recovery` | AEL-US18-01 |
| AEL-US19-01 | `.skillweave/worktrees/ael-us19-01` | `ael/ael-us19-01` | `docs/operations` | AEL-US09-02, AEL-US17-02, AEL-US18-02, AEL-BOOTSTRAP |
| AEL-US19-02 | `.skillweave/worktrees/ael-us19-02` | `ael/ael-us19-02` | `docs/operations` | AEL-US19-01 |

## Geprüfte Disjunktheit
- 51 Lanes, 34 verschiedene Schreibflächen; Prüfung paarweise auf Gleichheit/Präfix-Überlappung verschiedener Flächen: **0 Konflikte** .
- Flächen, die von mehreren Lanes geteilt werden, sind durch Abhängigkeitskette strikt sequenziell (nie parallel):
  - `cmd/ael`: AEL-BOOTSTRAP, AEL-INTEGRATE
  - `config/sources`: AEL-US01-01, AEL-US01-02
  - `storage/evidence`: AEL-US03-01, AEL-US03-02
  - `importers/files`: AEL-US05-01, AEL-US05-02
  - `collectors/backfill`: AEL-US06-01, AEL-US06-02
  - `importers/manual`: AEL-US07-01, AEL-US07-02
  - `collectors/byteplus`: AEL-US08-01, AEL-US08-02
  - `collectors/orchestration`: AEL-US09-01, AEL-US09-02
  - `collectors/runtime`: AEL-US10-01, AEL-US10-02
  - `collectors/forge`: AEL-US12-01, AEL-US12-02
  - `derive/engineering`: AEL-US13-01, AEL-US13-02
  - `derive/delta`: AEL-US14-01, AEL-US14-02
  - `reconcile/coverage`: AEL-US15-01, AEL-US15-02
  - `derive/cost`: AEL-US16-01, AEL-US16-02
  - `export/study`: AEL-US17-01, AEL-US17-02
  - `storage/recovery`: AEL-US18-01, AEL-US18-02
  - `docs/operations`: AEL-US19-01, AEL-US19-02

## Konfliktliste / Auflagen
- `cmd/ael`: BOOTSTRAP → INTEGRATE (sequenziell, per Abhängigkeit). Alle Lanes nutzen nur ihre Fläche; Manifeste/Lockfiles/`tests/run_all.py`/`tests/modules.json`/`pyproject.toml` nur BOOTSTRAP (Änderungsanfrage nötig).
- Fläche `collectors/forge` (US12) vs `collectors/forgejo`/`collectors/github` (Adapter): verschiedene Verzeichnisse, kein Präfix-Konflikt; `forge` darf nicht in Adapterverzeichnisse schreiben.
- CAP-*-Lanes schreiben nach `.skillweave/evidence/*` (disjunkt von Code); AEL-PILOT nach `.skillweave/evidence/pilot`; PREFLIGHT nach `.skillweave/runtime/preflight`.
- Lanes `AEL-CONTRACT-*` besitzen `schema/{core,collector,export}`; Exit-Code-Änderungen (ADR-0002) erfordern erneuten Contract-Lauf, nicht BOOTSTRAP.
- Slotgrenze unbelegt: konservativ 1–3 parallele Lanes je Batch (Checkpoint Batch 0/1).

Hinweis: Branch-/Worktreenamen stammen aus den Prompts (`Eigener Worktree`/`Branch`); Existenz wird erst bei Lane-Start vom Orchestrator geprüft.
