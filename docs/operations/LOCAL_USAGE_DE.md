# AI Engineering Ledger lokal betreiben

Diese Anleitung beschreibt den lokalen SQLite-first-MVP. Sie trennt bewusst zwischen:

- **automatisch abrufbaren Daten** aus offiziellen, read-only API-/CLI-Aufrufen,
- **manuellen Snapshots** aus Nutzungsoberflächen und
- **nicht verfügbaren Daten**, die im Ledger unbekannt bleiben müssen.

Die Befehle schreiben ausschließlich in die angegebene lokale Ledger-Datenbank. Provider-Zugangsdaten gehören in Umgebungsvariablen beziehungsweise bestehende CLI-Profile, niemals in Importdateien oder Git.

## 1. Installation und Ledger anlegen

Der vorgesehene Installationsweg für veröffentlichte Versionen ist Homebrew:

```bash
brew tap langevc/tap
brew install ael
ael setup
ael doctor
```

Bis der erste öffentliche Release verfügbar ist, kann AEL aus einem lokalen
Checkout mit Python 3.11 oder neuer installiert werden:

```bash
git clone https://github.com/LangeVC/ai-engineering-ledger.git
cd ai-engineering-ledger
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .

ael init "$HOME/.local/share/ael"
ael doctor "$HOME/.local/share/ael"
```

Die folgenden Beispiele verwenden:

```text
Ledger:  $HOME/.local/share/ael/ledger.db
Inbox:   $HOME/.local/share/ael/inbox/
```

Lege die Inbox lokal an und schütze sie, weil Provider-Antworten sensible Account- und Nutzungsdaten enthalten können:

```bash
mkdir -p "$HOME/.local/share/ael/inbox"
chmod 700 "$HOME/.local/share/ael" "$HOME/.local/share/ael/inbox"
```

## 2. Einheitlicher Import und Bericht

Jeder Import bekommt eine Quelle, einen stabilen lokalen Account-Alias und einen Datentyp. Der Alias ist kein Login und keine E-Mail-Adresse. Beispiele: `deepseek-main`, `bp01` bis `bp05`, `claude-pro-main`.

```bash
ael import \
  --database "$HOME/.local/share/ael/ledger.db" \
  --source SOURCE \
  --account ACCOUNT_ALIAS \
  --kind KIND \
  --input DATEI
```

Erneutes Importieren derselben Datei darf keine Messwerte verdoppeln. Das Ledger verwendet Inhalts- und Idempotenzschlüssel und meldet angenommene sowie bereits vorhandene Datensätze getrennt.

```bash
ael report providers \
  --database "$HOME/.local/share/ael/ledger.db" \
  --from 2026-10-01T00:00:00Z \
  --to 2026-11-01T00:00:00Z \
  --format table
```

Für maschinelle Weiterverarbeitung `--format json` verwenden. Summierbare Intervalle (zum Beispiel Tokens oder Kosten) und Gauge-/Snapshot-Werte (zum Beispiel Restguthaben oder Quotenauslastung) dürfen im Bericht nicht miteinander verrechnet werden.

## 3. Manuelle Dashboard-Snapshots

Claude Pro, Codex Plus und Google AI Pro/Antigravity stellen für den persönlichen Tarif keine hier belegte historische Usage-API bereit. Deshalb werden sichtbare Werte manuell und mit Beobachtungszeitpunkt erfasst. Nicht sichtbare Tokenzahlen oder Resetzeitpunkte werden **nicht geschätzt**.

Ein Snapshot ist eine lokale JSON-Datei. Beispiel für Claude Pro:

```json
{
  "source": "anthropic",
  "product": "Claude Pro",
  "account_alias": "claude-pro-main",
  "observed_at": "2026-10-27T12:00:00+01:00",
  "quota_metrics": {
    "quota_remaining_percent": "62",
    "reset_at": "2026-10-27T15:00:00+01:00",
    "window_type": "5h",
    "source_statement": "62% remaining"
  },
  "weekly_allotment": {
    "quota_remaining_percent": "48",
    "reset_at": null
  },
  "notes": "Manuell aus Settings > Usage abgelesen"
}
```

Import:

```bash
ael import --database "$HOME/.local/share/ael/ledger.db" \
  --source claude --account claude-pro-main --kind manual-snapshot \
  --input "$HOME/.local/share/ael/inbox/claude-2026-10-27.json"
```

### Claude Desktop – Pro

Quelle: Claude **Settings > Usage**. Erfasse die sichtbare kurze Nutzungsperiode und das Wochenfenster getrennt. Prozente bleiben Prozente. Ein unbekannter Reset bleibt `null`. Ein allgemeiner Claude-Datenexport ist kein Ersatz für einen Usage-/Abrechnungsnachweis.

### Codex – ChatGPT Plus

Quelle: ChatGPT **Settings > Usage**. Erfasse das sichtbare 5-Stunden- und Wochenfenster getrennt. Der Plus-Verbrauch ist über Codex-Oberflächen geteilt; er darf nicht als unabhängige Desktop-, CLI- und Web-Quote dreifach erfasst werden.

Codex verwendet dasselbe Template-Grundmuster mit:

```json
{
  "source": "openai",
  "product": "ChatGPT Plus / Codex",
  "account_alias": "codex-plus-main",
  "observed_at": "2026-10-27T12:00:00+01:00",
  "allowance_metrics": {
    "quota_remaining_percent": "71",
    "window_type": "5h",
    "window_label": "rolling_5h",
    "reset_at": null
  },
  "weekly_allotment": {
    "quota_remaining_percent": "55",
    "reset_at": null
  }
}
```

```bash
ael import --database "$HOME/.local/share/ael/ledger.db" \
  --source codex --account codex-plus-main --kind manual-snapshot \
  --input "$HOME/.local/share/ael/inbox/codex-2026-10-27.json"
```

### Antigravity – Google AI Pro

Quellen: die Antigravity-Anzeigen `/usage`, `/credits` beziehungsweise die Usage-/Credit-Anzeige in den Einstellungen. Quote und Credit-Overage sind getrennte Sachverhalte. Credits werden mit Einheit `credit`, nicht als Tokens oder Währung, gespeichert.

```json
{
  "source": "google",
  "product": "Google AI Pro / Antigravity",
  "account_alias": "antigravity-main",
  "observed_at": "2026-10-27T12:00:00+01:00",
  "credit_metrics": {
    "credits_consumed": "125",
    "credit_balance": "875",
    "unit": "credit"
  },
  "quota_metrics": {
    "quota_remaining_percent": "80",
    "window_type": "monthly",
    "reset_at": null
  }
}
```

```bash
ael import --database "$HOME/.local/share/ael/ledger.db" \
  --source antigravity --account antigravity-main --kind manual-snapshot \
  --input "$HOME/.local/share/ael/inbox/antigravity-2026-10-27.json"
```

Ein Screenshot kann lokal als Beleg aufbewahrt werden. Der MVP führt kein automatisches OCR aus und sendet Bilder nicht an externe Dienste. Im JSON nur den tatsächlich abgelesenen Wert eintragen.

## 4. DeepSeek – API und Credits

### Aktueller Kontostand

DeepSeek stellt den aktuellen API-Kontostand read-only über `GET /user/balance` bereit. Der API-Key bleibt in der Umgebung:

```bash
export DEEPSEEK_API_KEY='...'
curl --fail --silent --show-error \
  -H "Authorization: Bearer $DEEPSEEK_API_KEY" \
  https://api.deepseek.com/user/balance \
  -o "$HOME/.local/share/ael/inbox/deepseek-balance-2026-10-27.json"
unset DEEPSEEK_API_KEY

ael import --database "$HOME/.local/share/ael/ledger.db" \
  --source deepseek --account deepseek-main --kind deepseek-balance \
  --input "$HOME/.local/share/ael/inbox/deepseek-balance-2026-10-27.json"
```

Der Balance-Endpunkt liefert einen Snapshot, keine historische Verbrauchsreihe. Historische Daten benötigen vorhandene Exporte oder eigene, bereits aufgezeichnete Response-Usage.

### API-Response-Usage und historische Dateien

Bei eigenen DeepSeek-Aufrufen die `usage`-Struktur der Response unverändert als JSON sichern; sie kann Input-, Output-, Cache- und Reasoning-Tokens enthalten. Alternativ vorhandene DeepSeek-CSV-/JSON-/JSONL-Exporte importieren:

```bash
ael import --database "$HOME/.local/share/ael/ledger.db" \
  --source deepseek --account deepseek-main --kind deepseek-usage \
  --input "$HOME/.local/share/ael/inbox/deepseek-response.json"

ael import --database "$HOME/.local/share/ael/ledger.db" \
  --source deepseek --account deepseek-main --kind deepseek-csv \
  --input "$HOME/.local/share/ael/inbox/deepseek-history.csv"
```

## 5. BytePlus – fünf Coding-Plan-Pro-Accounts

Jeder Account benötigt ein eigenes ArkCLI-Profil: `bp01`, `bp02`, `bp03`, `bp04`, `bp05`. Jeder Aufruf muss `--profile` explizit enthalten; ein globaler Profilwechsel während eines Batch-Laufs ist nicht zulässig.

Für jeden Alias separat erfassen:

```bash
arkcli usage plan --product coding-plan --profile bp01 --format json \
  > "$HOME/.local/share/ael/inbox/byteplus-bp01-plan.json"

arkcli usage stats --start 2026-10-01 --profile bp01 --format json \
  > "$HOME/.local/share/ael/inbox/byteplus-bp01-stats.json"

arkcli billing list --start 2026-10 --product ModelArk_subscription \
  --profile bp01 --format json \
  > "$HOME/.local/share/ael/inbox/byteplus-bp01-billing.json"
```

Dann je Domäne importieren:

```bash
ael import --database "$HOME/.local/share/ael/ledger.db" \
  --source byteplus --account bp01 --kind byteplus-plan \
  --input "$HOME/.local/share/ael/inbox/byteplus-bp01-plan.json"

ael import --database "$HOME/.local/share/ael/ledger.db" \
  --source byteplus --account bp01 --kind byteplus-stats \
  --input "$HOME/.local/share/ael/inbox/byteplus-bp01-stats.json"

ael import --database "$HOME/.local/share/ael/ledger.db" \
  --source byteplus --account bp01 --kind byteplus-billing \
  --input "$HOME/.local/share/ael/inbox/byteplus-bp01-billing.json"
```

Den Ablauf für `bp02` bis `bp05` wiederholen. Coding-Plan-Quote, Inference-Usage und Billing bleiben getrennte Produktdomänen. Usage-Statistiken können 5–30 Minuten verzögert sein und ein einzelner Statistikzeitraum darf höchstens 31 Tage umfassen. Billing ist typischerweise T+1. Eine Billing-Antwort mit `is_truncated=true` gilt als unvollständig und darf nicht als vollständige Periode berichtet werden.

## 6. Kilo Code

Kilo liefert lokale Session- und Statistikdaten. Sensible Inhalte mit `--sanitize` aus einem Session-Export entfernen:

```bash
kilo session list --all --format json \
  > "$HOME/.local/share/ael/inbox/kilo-sessions.json"

kilo export SESSION_ID --sanitize \
  > "$HOME/.local/share/ael/inbox/kilo-session-SESSION_ID.json"

kilo stats --days 30 --models
```

Importiere den sanitisierten Session-Export:

```bash
ael import --database "$HOME/.local/share/ael/ledger.db" \
  --source kilo --account kilo-local --kind kilo-session \
  --input "$HOME/.local/share/ael/inbox/kilo-session-SESSION_ID.json"
```

Die derzeit belegte lokale `kilo stats`-CLI besitzt keine JSON-Formatoption. Ihre
Tabellenausgabe dient deshalb nur der manuellen Gegenprüfung und wird nicht als
maschinenlesbarer Import ausgegeben. `kilo-stats` ist nur für bereits vorhandene,
strukturierte JSON-Statistikartefakte zulässig; Textausgabe darf nicht heuristisch
als Messwert geparst werden.

Session-/Request-Werte und aggregierte `kilo stats` dürfen für denselben Zeitraum nicht addiert werden. Kilo-Kosten sind Ausführungsbeobachtungen; sie sind nicht automatisch identisch mit einem Provider-Abrechnungsbetrag oder einer Subscription.

## 7. Wiederkehrende Erfassung

Automatisiere zunächst nur read-only CLI-/API-Captures. Manuelle Dashboard-Werte bleiben ein bewusster, bestätigter Schritt. Ein Beispiel für die lokale Reihenfolge:

1. Provider-/CLI-Ausgabe atomar in die geschützte Inbox schreiben.
2. Mit `ael import` importieren.
3. Exit-Code und JSON-Ergebnis protokollieren.
4. Mit `ael report providers` die neue Datenlage kontrollieren.
5. Erst danach alte Inbox-Dateien nach der eigenen Aufbewahrungsrichtlinie entfernen.

Für Cron oder `launchd` nie API-Keys in die Crontab schreiben. Verwende bestehende Provider-Profile beziehungsweise einen geeigneten lokalen Secret Store. Die fünf BytePlus-Profile müssen unabhängig laufen; der Ausfall eines Accounts darf die anderen vier Importe nicht verwerfen.

## 8. Datenqualität und Datenschutz

- Zeitstempel immer mit Offset oder `Z` im RFC-3339-Format erfassen.
- Geldbeträge und Credits als exakte Dezimalzeichenfolge, nicht als Float, importieren.
- Account-Aliasse statt E-Mail-Adressen oder Provider-IDs verwenden.
- Keine API-Keys, Authorization-Header, Prompts, Chats oder Repository-Inhalte importieren.
- Unbekannte Werte bleiben `null`/unbekannt; `0` bedeutet nur ein belegtes Nullergebnis.
- Snapshot-/Gauge-Werte nicht summieren; Intervallsummen nur innerhalb identischer Quelle, Account, Metrik, Einheit und Periode aggregieren.
- Importquellen behalten Herkunftsmethode, Confidence, Evidence-Hash und Importzeitpunkt.
- Lokale Datenbank, Inbox, Backups und Exporte mit Dateirechten `0600` beziehungsweise Verzeichnisrechten `0700` schützen.

## 9. Was der MVP nicht behauptet

- Er rekonstruiert keine historischen Werte, die der Provider nicht bereitstellt.
- Er rechnet Prozente oder Credits nicht in erfundene Tokens um.
- Er setzt unbekannte Resetzeitpunkte nicht aus einem Tarifnamen her.
- Er setzt keinen erfolgreichen Live-Zugriff voraus: Credentials, Provider-Login und Netzverbindung werden erst beim jeweiligen Capture benötigt.
- Er behandelt Gateway-/Agent-Telemetrie nicht als Ersatz für Provider-Abrechnung und Provider-Abrechnung nicht als Ersatz für Laufzeitkontext.
