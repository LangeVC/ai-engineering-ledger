# ADR-0001 Sprache und Packaging
Status: akzeptiert (Unicorn-Routineentscheidung, AEL-BOOTSTRAP) · Datum 2026-10-26

## Entscheidung
- **Python (>=3.11, entwickelt mit 3.15) und nur Standardbibliothek zur Laufzeit** (`sqlite3`, `csv`, `hashlib`, `argparse`, `decimal`, `json`).
- Packaging mit **uv** (`pyproject.toml` + `uv.lock`, Dev-Gruppe: `jsonschema`, `referencing`). Einstieg `ael` = `ael.cli:main` (Quelle unter `cmd/ael`, Paketlayout wird in AEL-INTEGRATE festgelegt, Eigentümer `cmd/ael`).
- Contracts werden **nicht kopiert**: Ladeweg `$AEL_SCHEMA_DIR`, sonst `<repo>/schema`, sonst aufwärts gesuchtes `schema/` (siehe `tests/run_all.py`).
- Testharness: `python3 tests/run_all.py` (stdlib; Contract-Suiten haben eigenen Minimalvalidator), strikt mit `uv run --group dev python tests/run_all.py --strict`.

## Begründung
- Contract-Tests, Beleg-Hashing (DQ02/DQ03) und CSV-Guard existieren bereits in Python: kein Portierungsrisiko, byte-identische Hash-/Kanonisierungssemantik (sorted keys, ensure_ascii=False, Decimal).
- PRD: lokales Single-User-Ledger, SQLite, macOS/Linux, shelltauglich, OSS-Selbstnutzung durch Zweitperson: Python+SQLite sind überall vorhanden; keine Cloud, keine Serverkomponente.
- Stdlib-only Laufzeit minimiert Supply-Chain- und Secret-Risiko; `jsonschema` nur für Tests.

## Verworfen
- Go: einzelnes Binary attraktiv, aber alle Contract-Tests/Guards müssten portiert und Hash-Parität neu bewiesen werden; Decimal-Semantik umständlicher.
- Node: kein Vorteil, zusätzliche Toolchain, schwächere Decimal-/SQLite-Stdlib.

## Konsequenzen / Besitz
- Manifeste (`pyproject.toml`, `uv.lock`, `tests/run_all.py`, `tests/modules.json`) gehören ausschließlich AEL-BOOTSTRAP; später nur über BOOTSTRAP-Änderungsanfrage. `cmd/ael` gehört BOOTSTRAP, danach AEL-INTEGRATE (sequenziell, nie parallel).
- Neue Lane-Module schreiben nur in ihre Fläche; Lane-Tests innerhalb der Fläche, vom Harness nicht automatisch entdeckt (Registrierung = BOOTSTRAP-Änderung, Follow-up: Auto-Discovery `*/tests/test_*.py`).
- Python-3.15-Hinweis: `requires-python >=3.11` bewusst; `uv lock` löste mit CPython 3.13 auf.
