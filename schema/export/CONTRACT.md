# AEL Export- und CLI-Vertrag 0.1

Artefakte: `cli-result.schema.json` (`cli-json-result-and-exit-status@0.1`), `manifest.schema.json` + `columns.json` (`study-export-and-manifest@0.1`). JSON Schema draft 2020-12; sprachunabhängig. Prüfung: `python3 schema/export/tests/validate.py`.

## 1. CLI-Exitstatus (US-09.d)
**Status: bindend gemäß ADR-0002.** Die Werte 0/3/1/2 gelten bis dahin als Arbeitsstand.

| Exit | status | Bedeutung |
|---|---|---|
| 0 | `success` | Alle ausgeführten Läufe erfolgreich; manuelle Quellen (`skipped_due_manual`) gelten nicht als Fehler. |
| 3 | `partial` | Mindestens ein Lauf erfolgreich und mindestens einer `failed`. |
| 1 | `failure` | Kein Lauf erfolgreich bzw. Gesamtfehler (z. B. Auth, Konfiguration). |
| 2 | – | Aufruf-/Usage-Fehler; keine JSON-Pflicht, Fehlertext auf stderr. |
Maschinenausgabe: ein JSON-Objekt auf stdout mit `contract_version:"0.1"`, Source-/Account-**Aliase** (keine Secrets, Pfade, Inhalte), Retrybudget und letztem Fehlercode. `exit_code` muss zu `status` passen. Menschliche Darstellung darf sich ändern, Feldbedeutung und Exitwerte nicht.

## 2. Studienpaket (US-17)
Dateien: `dataset, providers, models, costs, git-activity, releases, reconciliation, provenance` (`.csv`) und `manifest.json`. Spalten in `columns.json`. Nicht verfügbare Kategorien: nur Headerzeile, im Manifest `coverage:"unavailable"` plus Hinweis; keine erfundenen Zeilen. Unbekannt ist leer, nie 0.
- **Einheiten:** Tokens = Tokens; Kosten dezimal mit ISO-4217-Währung; Zeit = UTC (`Z`), Quelloffset separat.
- **Zeitzone/Zeitraum:** Manifest nennt `timezone` (IANA-Name wie `America/Argentina/Buenos_Aires`, `Etc/GMT+1`, oder `UTC`); Filter halboffen `[from,to)`; keine 24-h-Tage-Annahme.
- **Datenstand:** `data_state.id/as_of`; früherer Stand wählbar (`explicitly_selected_earlier`); datenschutzgelöschte Evidenz: `reproducible:false`, nicht zurückgeholt.
- **Reproduzierbarkeit:** gleiche Daten + Parameter ⇒ bytegleiche CSVs (UTF-8, LF, Komma, RFC 4180, feste Zeilenordnung nach Schlüssel) und gleiche `sha256`/`content_hash`. `export_id` und `export_timestamp` sind aus der Inhaltsprüfung ausgeschlossen.
- **Redaktion:** nur Aliase; keine Secrets, persönlichen Pfade, Prompts, Chat-/Codeinhalte; Manifest erlaubt keine Zusatzfelder (`additionalProperties:false`). Belegexport ist separate explizite Option.

## 3. CSV-Darstellungsschutz (US-17.f)
Regel `formula-neutralization-prefix-apostrophe@0.1`.
- **Spaltentypen:** `text` wird geschützt. Strikt validierte Typen werden **nicht** verändert, weil ihr Format Formelanfänge ausschließt bzw. Abweichungen abgelehnt werden: `integer` `^-?\d+$` (nur dieser Typ darf mit `-` beginnen, Zahl bleibt Zahl), `decimal` `^-?\d+(\.\d+)?$`, `timestamp` `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$`, `date` `^\d{4}-\d{2}-\d{2}$`, `offset` `^[+-]\d{2}:\d{2}$` (**benannte Ausnahme**: beginnt legitim mit `+`/`-`, daher per Regex validiert statt geschützt; jeder andere Wert ist ein Exportfehler).
- **Textzellen:** Beginnt der Wert mit `=`, `+`, `-`, `@`, Tab, CR, LF, Leerzeichen **oder `'`**, wird ein `'` vorangestellt.
- **Injektivität/Rückweg:** Jede geschützte Zelle beginnt mit `'`; ein ungeschützter Wert beginnt nie mit `'`. Dekodierung: genau ein führendes `'` entfernen, falls vorhanden. Damit ist die Abbildung injektiv (`'abc` → `''abc`).
- **Leerraum/Newline:** Führendes Leerzeichen oder LF löst den Schutz aus (Tabellenprogramme können dahinter Formeln auswerten); eingebettete Newlines werden per RFC-4180-Quoting erhalten.
- Nur CSV; JSON behält den Originalwert unverändert. Das Manifest benennt die Regel.

## 4. Kompatibilität
Breaking Change (Spalte entfernt/umgedeutet/Einheit) ⇒ neue Exportversion; additive Änderungen nur mit Minor-Erhöhung.

## 5. Offen
Kein Live-Test; Spaltenkatalog ist ein Mindestsatz, Feinschema je Tabelle (Typprüfung aller Tabellen, Fixtures außer `dataset`) folgt mit Implementierung. Fixture-Hashes sind synthetisch.

Hinweis: Unicode-Leerraum (z.B. NBSP U+00A0), BOM und NUL sind nicht Teil des automatischen CSV-Neutralisierungspräfixes.
