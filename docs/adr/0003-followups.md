# ADR-0003 Follow-ups aus Reviews (Auflagen, nicht in BOOTSTRAP umgesetzt)
1. **EXPORT Zellregex**: Zellprüfungen mit `re.fullmatch`, Ziffernklassen explizit `[0-9]` statt `\d` (Python-`\d` matcht Unicode-Ziffern, z. B. Arabisch-Indisch; Schema-/JSON-Schema-Regex ECMA beachten). Zusätzliche negative Fixture mit Unicode-Ziffern. Eigner: EXPORT-Contract-Lane.
2. **EVENT README**: Widerspruch Zeilen ~13 und ~22 (Aussage zu jsonschema/Validator: "own minimal validator" vs. optionaler jsonschema-Konsistenzcheck) auflösen. Eigner: EVENT-Contract-Lane.
3. **COLLECT README Konsumentenpflicht**: ausdrücklich festhalten, dass Konsumenten (Collector-Adapter, US-02/08/09/10/12) Schemavalidierung UND semantische Regeln (success↔process.exit_code, finished>=started, Truncation) selbst ausführen müssen; Schema allein genügt nicht. Eigner: COLLECT-Contract-Lane.
4. Exit-Code-Schemaänderungen: siehe ADR-0002.
5. Harness: Auto-Discovery von Lane-Tests; Regeneration `--from-prd --repo` (laut Checkpoint) nach Merge.
