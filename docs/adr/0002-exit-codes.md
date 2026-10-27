# ADR-0002 Kanonische Exit-Code-Tabelle
Status: akzeptiert (Unicorn) · Datum 2026-10-26

## Entscheidung
Der **Prozessexit der CLI** folgt dem EXPORT/CLI-Vertrag (US-09.d):

| Exit | status | Bedeutung |
|---|---|---|
| 0 | success | alle ausgeführten Läufe erfolgreich (manuelle Quellen kein Fehler) |
| 1 | failure | kein Lauf erfolgreich / Gesamtfehler (Auth, Konfiguration) |
| 2 | – | Usage-Fehler, Text auf stderr, kein JSON-Pflicht |
| 3 | partial | mindestens ein Lauf erfolgreich, mindestens einer failed |

Keine weiteren Prozessexitwerte in 0.1 (Signale/Crash: Shell-Konvention 128+n, nicht Teil des Vertrags).

## COLLECT-Mapping
COLLECT `exit_status` (0/10/20) ist **kein Prozessexit** mehr. Mapping: 0→0 (success), 10 partial_failure→3 (partial), 20 failure→1 (failure). Das Feld wird durch den semantischen `status` ersetzt; Ursachenklassen laufen über `error_class` (error.schema.json), nicht über Exitzahlen.

## Änderungsliste Contract-Schemas (NICHT von BOOTSTRAP ausgeführt; Eigner: Contract-Lane/Integrator, Lock/Hash neu)
1. `schema/collector/run.schema.json`: `exit_status` (enum 0/10/20, "PROVISIONAL") → `status` enum `success|partial|failure` + (falls Prozessexit gespiegelt) `cli_exit_code` enum `[0,3,1]` mit if/then-Kopplung success↔0, partial↔3, failure↔1. Die bestehende Regel "success ⇒ process.exit_code==0" bezieht sich auf den **Kindprozess**; im Text von `process.exit_code` klarstellen.
2. Fixture `run-status-exit-mismatch.json` auf neue Kopplung umstellen; neue invalide Fixtures: partial mit exit 0, failure mit exit 3, Wert 10/20 wird abgelehnt; gültige Fixtures auf neue Felder migrieren.
3. `schema/collector/README.md` Abschnitt "Exit status (PROVISIONAL)": durch Verweis auf ADR-0002 ersetzen, PROVISIONAL streichen; `run.schema.json` description ebenso.
4. `schema/export/CONTRACT.md` §1: Zeile "vorläufig, bindend erst nach BOOTSTRAP-ADR" → "bindend gemäß ADR-0002"; **Achtung**: `export/tests/validate.py` Zeile ~103 prüft genau diesen Text ("CONTRACT exit table marked provisional") → Test gleichzeitig anpassen.
5. `schema/export/cli-result.schema.json`: Exit-Enum [0,1,3] unverändert; Usage (2) bleibt außerhalb des JSON-Schemas (dokumentieren).
6. `error.schema.json`: `error_class` als Ursachenachse bestätigen (keine Exitwerte); Tabelle der Klassen→Standard-status in README.
7. Locks/Hashes (`core/schema.lock.json` falls betroffen, Collector-Hashes) und Patch-SHA neu erzeugen; Re-Review.

Abhängige Lanes (US-09 Orchestrierung, INTEGRATE) dürfen erst gegen die geänderten Schemas bauen; bis dahin gilt diese ADR als bindend, die Schemas als überholt.
