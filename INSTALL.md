# Installationsanleitung

Diese Anleitung richtet den **Telegram-Bot** ein. Sie gilt für **Debian und Ubuntu** (getestet unter Debian mit Python 3.13). Alle Befehle als `root` ausführen oder mit `sudo`.

## Voraussetzungen

- Ein Bedrock Dedicated Server läuft bereits als **systemd-Dienst**. Den Dienstnamen findest du so:
  ```bash
  systemctl list-units --type=service | grep -i -E "bedrock|minecraft"
  ```
  In den Beispielen heißt er `minecraft-bedrock`, das Serververzeichnis `/opt/bedrock`. Das Serververzeichnis ist der Ordner mit `bedrock_server`, `worlds/` und `server.properties`: Der Bot sichert und aktualisiert genau diesen Ordner.
- Ein Telegram-Account und eine Telegram-Gruppe
- Internetzugang vom Server (Telegram-API, bei `/update` auch Mojang)

---

## 1. Pakete installieren

```bash
apt update
apt install -y git python3 python3-venv
```

`python3-venv` ist wichtig, sonst scheitert das Anlegen der virtuellen Umgebung mit "ensurepip is not available".

## 2. Telegram-Bot anlegen

### 2.1 Bot erstellen

1. In Telegram mit [@BotFather](https://t.me/BotFather) chatten und `/newbot` senden. Namen und Benutzernamen vergeben.
2. Den **Token** notieren (Format `123456789:AA...`). Er ist ein Passwort: niemals veröffentlichen oder einchecken.

### 2.2 Bot in die Gruppe holen

1. Den Bot in die Telegram-Gruppe einladen.
2. In der Gruppe einen Befehl senden, z. B. `/help`. Der Bot hat standardmäßig den Privacy Mode und sieht nur `/befehle`, das reicht hier.

### 2.3 Gruppen-ID herausfinden

```bash
curl "https://api.telegram.org/bot<TOKEN>/getUpdates"
```

In der Antwort steht bei `"chat":{"id":-...` die **Gruppen-ID** (eine negative Zahl).

Ist `"result":[]` leer: Ist der Bot wirklich in der Gruppe? Dann nochmal einen `/befehl` in die Gruppe schicken und den `curl` wiederholen.

### 2.4 User-IDs herausfinden

Deine numerische ID zeigt dir [@userinfobot](https://t.me/userinfobot) (oder `"from":{"id":...` im `getUpdates`-Ergebnis). Du brauchst sie für zwei Berechtigungsstufen:

- `ALLOWED_USER_IDS`: dürfen `/restart` und `/backup`
- `ADMIN_USER_IDS`: dürfen zusätzlich `/update` und `/restore`

## 3. Bot installieren

### 3.1 Code holen

```bash
git clone https://github.com/Wulfga/Bedrock-Telegram-Bot.git /opt/telegram-bedrock-bot
cd /opt/telegram-bedrock-bot
```

### 3.2 Python-Umgebung

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

### 3.3 Konfiguration

```bash
cp config.example.env .env
chmod 600 .env
nano .env
```

Mindestens diese Werte setzen:

```
BOT_TOKEN=<Token aus 2.1>
GROUP_CHAT_ID=<Gruppen-ID aus 2.3>
ALLOWED_USER_IDS=<deine ID>,<weitere IDs>
ADMIN_USER_IDS=<deine ID>
```

Weicht dein Dienstname oder Serverpfad ab, außerdem anpassen: `SERVICE_NAME`, `SERVER_DIR`, `COMMAND_FIFO_PATH`.

Wichtig: In der `.env` **keine Anführungszeichen** um Werte und keine Leerzeichen um das `=`. Alle weiteren Optionen (Backups, geplanter Neustart, Pfade) sind in `config.example.env` kommentiert.

### 3.4 Als systemd-Dienst einrichten

```bash
cp telegram-bedrock-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now telegram-bedrock-bot
```

Liegt der Bot nicht unter `/opt/telegram-bedrock-bot`, vorher in der `.service`-Datei `WorkingDirectory`, `EnvironmentFile` und `ExecStart` anpassen.

### 3.5 Prüfen

```bash
systemctl status telegram-bedrock-bot
journalctl -u telegram-bedrock-bot -f
```

Im Log sollten `Bot gestartet.` und `Log-Watcher gestartet` stehen. Dann in der Telegram-Gruppe:

```
/status
```

Erwartet: "🟢 Server online" mit Spieleranzahl und Version.

---

## 4. Optional: Ankündigungen im Spiel-Chat

Bei `/restart`, `/update` und `/restore` kann der Bot Countdown-Hinweise in den Spiel-Chat schicken (`say ...`). Dafür braucht er einen Weg, Befehle an die Server-Konsole zu senden: eine Named Pipe (FIFO), die der Server als Eingabe nutzt. Ohne diesen Schritt funktioniert alles, nur ohne Ingame-Meldung.

**Das ändert, wie dein Bedrock-Server gestartet wird. Kurze Downtime beim Neustart des Servers.**

```bash
cp /opt/telegram-bedrock-bot/run-with-fifo.sh /opt/bedrock/
chmod +x /opt/bedrock/run-with-fifo.sh
```

Liegt dein Server nicht unter `/opt/bedrock`, `SERVER_DIR` am Anfang des Skripts anpassen. Dann in der Unit des Bedrock-Servers (`/etc/systemd/system/minecraft-bedrock.service`) die `ExecStart`-Zeile ersetzen:

```
ExecStart=/opt/bedrock/run-with-fifo.sh
```

Anwenden:

```bash
systemctl daemon-reload
systemctl restart minecraft-bedrock
```

Der Bot erwartet die FIFO unter `/opt/bedrock/command_input` (`COMMAND_FIFO_PATH` in der `.env`).

Test, wenn jemand online ist:

```bash
echo "say Test" > /opt/bedrock/command_input
```

---

## 5. Backup, Update und Restore

Die drei Funktionen arbeiten auf dem **Serververzeichnis** (`SERVER_DIR`, Standard `/opt/bedrock`). Der Bot läuft dafür als `root` und braucht Schreibrechte auf `SERVER_DIR`, `BACKUP_DIR` und `UPDATE_DIR`. Die beiden letzten Ordner legt er bei Bedarf selbst an.

### Backup

| | |
|---|---|
| Manuell | `/backup` (`ALLOWED_USER_IDS`) |
| Automatisch | alle `BACKUP_INTERVAL_HOURS` Stunden (Standard 24), Ergebnis wird in die Gruppe gemeldet |
| Ablage | `BACKUP_DIR` (Standard `/opt/bedrock-backups`), Dateien `backup_<Datum>_<Uhrzeit>.tar.gz` |
| Aufbewahrung | die letzten `BACKUP_RETENTION_COUNT` Backups (Standard 7), ältere werden automatisch gelöscht |
| Inhalt | das komplette Serververzeichnis (Welten, Konfiguration, Server-Dateien) |

Hinweis: `/backup` und das automatische Backup laufen **bei laufendem Server**. Eine Welt, in die gerade geschrieben wird, kann dabei inkonsistent gesichert werden. Die Backups vor `/update` und `/restore` werden dagegen immer bei gestopptem Server erstellt. `BACKUP_DIR` darf nicht innerhalb von `SERVER_DIR` liegen.

Die Backups liegen auf demselben System wie der Server. Für echten Schutz zusätzlich regelmäßig auf ein anderes System kopieren.

### Update (`/update`, nur `ADMIN_USER_IDS`)

Ablauf:

1. Fragt bei Mojang die aktuelle Linux-Version ab und vergleicht sie mit der laufenden. Ist sie gleich, passiert nichts ("Bereits aktuell").
2. Lädt die neue ZIP nach `UPDATE_DIR` (Standard `/opt/bedrock-updates`). Schlägt das fehl, bleibt der Server unberührt.
3. Sind Spieler online, gibt es einen Countdown (`RESTART_COUNTDOWN_SECONDS`, Standard 60 s) mit Hinweisen im Spiel-Chat, falls die FIFO aus Abschnitt 4 eingerichtet ist.
4. Stoppt den Server, erstellt ein Backup, entpackt das Update und startet den Server.

**Diese Pfade werden dabei nie überschrieben** (`UPDATE_PROTECTED_PATHS`):

```
worlds, permissions.json, allowlist.json, server.properties,
resource_packs, behavior_packs, valid_known_packs.json
```

Liegen bei dir weitere Dateien, die erhalten bleiben sollen, hänge sie kommagetrennt in der `.env` an. Der Wert in der `.env` ersetzt dabei die gesamte Standardliste, also die Standardeinträge mit angeben.

Schlägt Backup oder Entpacken fehl, **bleibt der Server bewusst gestoppt** und der Bot meldet den Fehler in der Gruppe. Das Backup ist dann vorhanden. Während Update, Restore und Neustart pausiert die Offline-Erkennung (kein Fehlalarm).

Vor dem allerersten `/update` einmal `/backup` auslösen und prüfen, dass die Datei in `BACKUP_DIR` liegt.

### Restore (`/restore`, nur `ADMIN_USER_IDS`)

```
/restore                 Liste der vorhandenen Backups
/restore latest          neuestes Backup einspielen
/restore <dateiname>     bestimmtes Backup einspielen
```

Ablauf: Countdown (falls Spieler online sind), Server stoppen, **Sicherheitsbackup des aktuellen Stands**, Backup über das Serververzeichnis entpacken, Server starten.

`/restore` überschreibt Welten **und** Konfigurationsdateien mit dem Stand aus dem Backup. Dateien, die es jetzt gibt, im Backup aber nicht, werden nicht gelöscht. Es gibt keine Rückfrage: `ADMIN_USER_IDS` nur an vertrauenswürdige Personen vergeben. Das Sicherheitsbackup zählt zur Aufbewahrungsgrenze, bei vielen Wiederherstellungen hintereinander werden also ältere Backups entsprechend früher gelöscht.

### Berechtigungen im Überblick

| Befehl | Wer |
|---|---|
| `/restart`, `/backup` | `ALLOWED_USER_IDS` |
| `/update`, `/restore` | `ADMIN_USER_IDS` (leer = wie `ALLOWED_USER_IDS`) |

---

## 6. Funktionstest

| Test | Erwartung |
|---|---|
| `/status` | Server online, Version, Spieler |
| Jemand joint | Push "🟢 Name ist dem Server beigetreten" |
| `/spieler` | zeigt die Online-Spieler |
| `/backup` | Backup-Datei unter `/opt/bedrock-backups` |
| `/restart 10` (mit Spielern online) | Countdown, dann Neustart |
| `/update` | "Bereits aktuell" oder kompletter Update-Ablauf |

---

## Bot aktualisieren

```bash
cd /opt/telegram-bedrock-bot
git pull
./venv/bin/pip install -r requirements.txt
systemctl restart telegram-bedrock-bot
```

`.env` und `stats.db` bleiben dabei unberührt (per `.gitignore` ausgeschlossen).

---

## Fehlersuche

| Problem | Ursache / Lösung |
|---|---|
| `ensurepip is not available` | `apt install python3-venv`, den `venv/`-Ordner löschen, neu anlegen |
| `pip: command not found` | venv nicht aktiv: `./venv/bin/pip` verwenden |
| `KeyError: 'BOT_TOKEN'` im Log | `.env` fehlt, heißt anders (muss `.env` heißen, nicht `config.example.env`) oder der `EnvironmentFile`-Pfad stimmt nicht |
| Bot antwortet nicht in der Gruppe | Bot in der Gruppe? Token korrekt? `journalctl -u telegram-bedrock-bot -n 50` |
| `getUpdates` liefert `[]` | Nach dem Hinzufügen des Bots erst einen `/befehl` in der Gruppe senden |
| `/status` zeigt "offline" | Läuft der Dienst (`systemctl is-active <SERVICE_NAME>`)? Stimmt `SERVICE_NAME` in der `.env`? |
| `/status` zeigt "Version: unbekannt" | Die Startzeile `Version: ...` fehlt im Journal. Nach einem Neustart des Servers erscheint sie wieder. |
| `/status` zeigt Warnung zum TCP-Port | Der Server nimmt am `SERVER_PORT` (TCP) keine Verbindungen an. Die Warnung lässt sich mit `STATUS_TCP_CHECK=false` abschalten. |
| `/spieler` leer, obwohl jemand online ist | Bot wurde nach dem Join neu gestartet: Spieler kurz neu verbinden. Sonst Log-Format prüfen: `journalctl -u minecraft-bedrock \| grep -i "player connected"` |
| Keine Ingame-Ankündigung beim Neustart | Server läuft nicht über `run-with-fifo.sh` (Abschnitt 4) oder `COMMAND_FIFO_PATH` stimmt nicht |
| Fehlalarm "Server offline" bei `/update` | Bot-Version prüfen (`git pull`), ältere Versionen kannten den Wartungsmodus nicht |

## Deinstallation

```bash
systemctl disable --now telegram-bedrock-bot
rm /etc/systemd/system/telegram-bedrock-bot.service
systemctl daemon-reload
rm -rf /opt/telegram-bedrock-bot
```

Danach den Token bei @BotFather mit `/revoke` ungültig machen oder den Bot mit `/deletebot` löschen.
