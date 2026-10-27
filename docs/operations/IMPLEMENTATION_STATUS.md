# Implementierungsstatus v0.1

Stand: 2026-10-27. Basis-SHA vor den uncommitteten Arbeiten:
`166a02dd5052c7e25770745b47d8aac366480307`.

## Implementiert und fixture-geprüft

- PRD US-01 bis US-10 aus den übernommenen Batches einschließlich der
  US-10.d-ID-Invariante.
- US-11: belegstärkenbasierte Korrelation, sichtbare nicht zugeordnete Menge,
  genau eine Council-Summenebene.
- US-12: Allowlist-/Zeitraumgrenze, API-/Git-Fallbackstatus, Pagination-Gate,
  idempotente PR-/Release-Aktualisierung und historische Erreichbarkeit.
- US-13: Mirror-Deduplizierung nur nach bestätigter Repo-Äquivalenz,
  Committer-Zeitbasis und Identitätsalias.
- US-14: kompatibilitätsgebundene, versionierte Delta-/Reset-Ableitung.
- US-15: scope-strikte Provider/Runtime-Coverage ohne Billingbehauptung.
- US-16: getrennte Kostenarten, Währungen, Cashflow/Verbrauch und exakte Decimalwerte.
- US-17: deterministisches Studienpaket, Coverage-/Provenance-Manifest,
  CSV-Injektionsschutz und datensparsamer Standardexport.
- US-18: POSIX-Rechte, Redaction, Integritätsbackup/-restore, atomische Migration,
  Löschvorschau und inhaltsfreier Löschvermerk.
- US-19: lokaler Betriebsweg, Beispieladapter, Plattform-/Lizenz-/Security-Status.
- CLI-Wiring: `init`, `doctor`, `collect`, `import`, `snapshot`,
  `reconcile`, `export`, `backup`, `restore`, `delete-preview`, `delete`.

`pytest -q`: 201 bestanden, 3 erwartete Live-Skips.
`python3 tests/run_all.py`: Schema-, Lock-, Collector-, Export- und
Anwendungsprüfungen.

## HOLD – nicht als bestanden behauptet

- Live-Abnahmen der neun Quellen ohne freigegebene Credentials bzw. reale Exporte.
- Der siebentägige Betriebspilot.
- NF01–NF03 mit den vollständigen Zieldatenmengen; die Fixture-Suite ist kein
  Leistungsnachweis.
- Öffentliche OSS-Freigabe: Lizenzentscheidung, SECURITY.md, Dependency-/SBOM-Prüfung.

Der Stand ist daher **ready for credentialed integration/pilot**, nicht
`release complete` und nicht öffentlich veröffentlicht.
