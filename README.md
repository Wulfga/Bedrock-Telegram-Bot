# Telegram-Bot für Minecraft Bedrock Dedicated Server

Ein Telegram-Bot, der einen Minecraft Bedrock Server überwacht und per Chat steuerbar macht: Status und Spielerliste abfragen, Spielzeiten auswerten, Server mit Countdown neustarten, Backups anlegen, auf die neueste Version aktualisieren und Backups wiederherstellen. Dazu kommen automatische Meldungen in die Gruppe, wenn jemand joint, der Server ausfällt oder im Log Fehler auftauchen.

## Funktionen

### Abfragen (für alle)

| Befehl | Was er macht |
|---|---|
| `/status` | Online-Status, Spieleranzahl (aktuell/maximal) und Serverversion |
| `/spieler` | Wer ist gerade online, mit der bisherigen Dauer der Sitzung |
| `/playtime <Name>` | Gesamtspielzeit eines Spielers (eine laufende Sitzung wird mitgezählt) |
| `/top` | Rangliste der Top 5 nach Spielzeit |
| `/error [warn] [anzahl]` | Fehler seit dem letzten Serverstart aus dem Server-Log, gleichartige Meldungen zu Gruppen mit Anzahl zusammengefasst, häufigste zuerst (`/error warn` zeigt die Warnungen, `/error 30` mehr Einträge) |
| `/help` | Übersicht aller Befehle |

### Steuerung (`ALLOWED_USER_IDS`)

| Befehl | Was er macht |
|---|---|
| `/restart [sekunden]` | Startet den Server neu. Sind Spieler online, gibt es einen Countdown mit Ankündigungen im Spiel-Chat (bei 30, 10, 5, 4, 3, 2 und 1 Sekunden). Ist niemand online, startet er sofort. Die Dauer ist frei wählbar, z. B. `/restart 30`. |
| `/backup` | Legt sofort ein Backup des Serververzeichnisses an und meldet die Dateigröße |

### Administration (`ADMIN_USER_IDS`)

| Befehl | Was er macht |
|---|---|
| `/update` | Prüft bei Mojang die neueste Linux-Version. Ist sie neuer als die laufende: laden, Countdown, Server stoppen, Backup, einspielen, Server starten. Ist sie gleich, passiert nichts. |
| `/restore` | Zeigt die vorhandenen Backups |
| `/restore latest` | Spielt das neueste Backup ein |
| `/restore <dateiname>` | Spielt ein bestimmtes Backup ein |

Beide Befehle erzeugen vor dem Eingriff automatisch ein Backup des aktuellen Stands. Bei `/update` bleiben `worlds/`, `permissions.json`, `allowlist.json`, `server.properties` und die Resource-/Behavior-Packs unangetastet (`UPDATE_PROTECTED_PATHS`).

### Automatische Funktionen

| Funktion | Verhalten | Einstellung |
|---|---|---|
| Join/Leave-Meldung | Push in die Gruppe mit Spielernamen: „🟢 Name ist dem Server beigetreten“ bzw. „🔴 Name hat den Server verlassen (Session: 1h 20m)“ | `LOG_MODE` |
| Startzusammenfassung | Nach jedem Serverstart eine Zeile mit Version und Anzahl der Fehler/Warnungen beim Start („keine Fehler oder Warnungen“ oder Verweis auf `/error`). Einzelne Meldungen beim Start werden nicht in die Gruppe geschickt. | `LOG_MODE` |
| Fehler-Alarm | Fehler, die nach dem Start im Server-Log auftauchen, werden einzeln gemeldet; unmittelbar aufeinanderfolgende gleiche Meldungen (ohne Zeitstempel verglichen) nur einmal. Warnungen werden nur gesammelt. | `LOG_MODE`, `ERROR_ALERTS_RUNTIME` |
| Ausfall-Erkennung | Meldet, wenn der Serverdienst nicht mehr läuft (z. B. Absturz) und wenn er wieder online ist. Pausiert während Neustart, Update und Restore, damit es keinen Fehlalarm gibt. | `POLL_INTERVAL` |
| Automatisches Backup | Sichert in festem Abstand (gerechnet ab Bot-Start) und meldet das Ergebnis in die Gruppe. Alte Backups werden aufgeräumt. | `BACKUP_INTERVAL_HOURS`, `BACKUP_RETENTION_COUNT`, `BACKUP_DIR` |
| Geplanter Neustart | Optional: täglicher Neustart zu fester Uhrzeit mit Countdown, auf Wunsch übersprungen, wenn niemand online ist | `SCHEDULED_RESTART_*` (standardmäßig aus) |
| Spielzeit-Statistik | Jede beendete Sitzung wird in einer lokalen SQLite-Datenbank gespeichert und speist `/playtime` und `/top` | `STATS_DB_PATH` |
| Wiedererkennung nach Bot-Neustart | Liest beim Start das Server-Log, damit `/spieler` auch Leute kennt, die schon vorher online waren (nur `LOG_MODE=journalctl`) | – |
| Ingame-Ankündigungen | Countdown-Hinweise erscheinen im Spiel-Chat, wenn der Server über `run-with-fifo.sh` gestartet wird (optional) | `COMMAND_FIFO_PATH` |

### Schutzmechanismen

- Zwei Berechtigungsstufen: `/restart` und `/backup` für `ALLOWED_USER_IDS`, `/update` und `/restore` nur für `ADMIN_USER_IDS`. Unberechtigte bekommen eine Absage.
- Backup **vor** jedem Update und jeder Wiederherstellung, bei gestopptem Server.
- Bricht ein Schritt ab (Backup, Entpacken, Start), bleibt der Server gestoppt und der Bot meldet den Fehler, statt mit einem halben Zustand weiterzumachen.
- Schlägt der Download beim Update fehl, wird der Server gar nicht erst angefasst.
- Alle Einstellungen, auch Token und IDs, liegen in einer `.env` außerhalb der Versionsverwaltung.

Hinweis zu den Rechten: Die Abfragen sind nicht auf die Gruppe beschränkt. Wer dem Bot schreiben kann, bekommt dort Status, Spielerliste und Statistiken. Steuern können nur die eingetragenen User-IDs.

## Voraussetzungen

- Debian oder Ubuntu mit systemd; der Bedrock Server läuft bereits als systemd-Dienst
- Python 3.10+ (`python3-venv`)
- Server-Start über `run-with-fifo.sh`, damit der Bot Ankündigungen (`say ...`) in den Spiel-Chat schicken kann (optional, ohne FIFO laufen Countdowns trotzdem, nur ohne Ingame-Hinweis)

## Einrichtung

Die vollständige Anleitung (Pakete, Bot bei Telegram anlegen, Gruppen- und User-IDs, systemd, Funktionstest, Fehlersuche) steht in **[INSTALL.md](INSTALL.md)**.

Kurzfassung, wenn du Token, Gruppen-ID und User-IDs schon hast:

```bash
apt install -y git python3 python3-venv
git clone https://github.com/Wulfga/Bedrock-Telegram-Bot.git /opt/telegram-bedrock-bot
cd /opt/telegram-bedrock-bot
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp config.example.env .env && chmod 600 .env && nano .env
cp telegram-bedrock-bot.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now telegram-bedrock-bot
```

## Konfiguration

Alle Einstellungen laufen über Umgebungsvariablen, siehe [`config.example.env`](config.example.env). Pflicht sind `BOT_TOKEN` und `GROUP_CHAT_ID`.

## Sicherheit

- **`.env` niemals einchecken.** Sie ist per `.gitignore` ausgeschlossen. Den Bot-Token bei Verdacht auf Leak über @BotFather mit `/revoke` erneuern.
- Der Bot läuft standardmäßig als `root`, weil er `systemctl` und Backups/Updates im Serververzeichnis braucht. Wer das nicht möchte, nutzt einen eigenen Systemnutzer mit passenden Rechten auf `SERVER_DIR`/`BACKUP_DIR`, einer sudoers-Regel für `systemctl start|stop|restart <dienst>` und Mitgliedschaft in der Gruppe `systemd-journal`.
- `/update` und `/restore` sind destruktiv: nur vertrauenswürdige Personen in `ADMIN_USER_IDS` aufnehmen.
- `/update` lädt die Server-ZIP von Mojangs offizieller Download-API. Du bist selbst dafür verantwortlich, die [Minecraft EULA](https://www.minecraft.net/eula) einzuhalten.

## Grenzen

- Seit Bedrock 1.26.51 nutzt der Server NetherNet und beantwortet die frühere UDP-Abfrage nicht mehr. Der Bot fragt den Server deshalb nicht ab, sondern liest den Zustand aus dem Dienststatus (systemd) und dem Server-Log: online = Dienst läuft, Spieler und Namen aus `Player connected/disconnected`, Version aus der Startzeile `Version: ...`, Spielerlimit aus `server.properties`. Weicht das Logformat in künftigen Versionen ab, müssen `JOIN_LEAVE_RE`, `VERSION_RE` und `SERVER_STARTED_RE` in `bot.py` angepasst werden.
- Ein Server, der hängt, aber als Dienst weiterläuft, wird nicht als offline erkannt.
- Spielerchat des Servers lässt sich ohne Behavior-Pack/Scripting nicht in Telegram spiegeln, daher gibt es keine Chat-Bridge.
- Spielzeit-Statistik zählt erst ab Installation; läuft der Bot nicht, werden Sessions nicht erfasst.

## Lizenz

MIT, siehe [LICENSE](LICENSE). Kein offizielles Mojang/Microsoft-Produkt.
