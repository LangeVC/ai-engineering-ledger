# AI Engineering Ledger v0.1 – Betrieb

Die v0.1 ist eine lokale, SQLite-first Single-User-CLI. Alle Beispiele sind synthetisch.
Echte Zugangsdaten gehören ausschließlich in externe Secret-Referenzen oder
Umgebungsvariablen; sie werden weder in Konfiguration noch Fixtures gespeichert.

## Installation und Start

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/ael init ./ael-data
.venv/bin/ael doctor ./ael-data
```

Unterstützt werden Python 3.11+ auf aktuellen macOS- und Linux-Systemen. Windows ist in
v0.1 nicht verifiziert. Die Laufzeit hat keine Drittanbieterabhängigkeiten.

## Import und manueller Snapshot

Dateiimporte werden zunächst mit `importers/files` erkannt und über
`importers/mapping` normalisiert. Ein manueller, synthetischer Snapshot:

```sh
ael snapshot --source example --account demo --metric tokens --value 120 \
  --unit tokens --observed-at 2026-10-27T12:00:00Z --output snapshot.json
```

Screenshots bleiben außerhalb des Standardexports. Zu jeder manuellen Eingabe gehören
Beobachtungszeit, Einheit und Provenance.

## Geplanter Betrieb

Cron ruft dieselben idempotenten Collector-Kommandos auf wie ein manueller Lauf. Beispiel:

```cron
17 * * * * cd /srv/ael && .venv/bin/ael doctor /var/lib/ael
```

Providerläufe müssen pro Account isolierte Profile verwenden. Ein Teilfehler ergibt
Prozessexit 3, ein Gesamtfehler 1. Live-Erfassung wird erst nach expliziter Freigabe und
vorhandenen Credentials aktiviert.

## Backup, Restore und Export

```sh
ael backup --database ./ael-data/ledger.db --output ./backup-2026-10-27
ael restore --backup ./backup-2026-10-27 --output ./restored
ael export --input synthetic-study.json --output ./study \
  --from 2026-10-01 --to 2026-11-01 --timezone Europe/Berlin \
  --study-cutoff 2026-11-01T00:00:00Z
```

Backup und Restore prüfen SHA-256-Integrität. Exporte enthalten standardmäßig keine
Secrets, Prompts, Codeinhalte, persönlichen Pfade oder Screenshots.

## Beispieladapter

`example_adapter.py` zeigt Capability, Validierung, Erfassung und Normalisierung ohne
faigate, Grafana oder andere Laufzeitabhängigkeiten.

## OSS- und Security-Status

- Lizenzwahl: **noch offen (NOASSERTION)**. Vor einer öffentlichen Veröffentlichung muss
  eine LICENSE-Datei ergänzt und freigegeben werden.
- Dependency-Lizenzen: keine Runtime-Dependencies; die Dev-Abhängigkeiten
  `jsonschema` und `referencing` müssen vor Veröffentlichung im SBOM geprüft werden.
- Sicherheitsmeldungen: bis zur Veröffentlichung privat an den Repository-Owner; eine
  öffentliche SECURITY.md und Supportfrist sind noch festzulegen.
- Unterstützte Plattformen: macOS/Linux, Python 3.11+; Windows nicht verifiziert.

Diese offenen Punkte verhindern eine Behauptung „öffentlich releasebereit“, nicht die
lokale Pilotvorbereitung.
