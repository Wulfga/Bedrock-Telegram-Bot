#!/usr/bin/env python3
"""
Telegram-Bot zur Steuerung/Überwachung eines Minecraft Bedrock Servers.

Befehle:
  /status    - Serverstatus (online/offline, Spieleranzahl, Version)
  /spieler   - Wer ist aktuell online
  /restart   - Neustart mit Countdown-Ankündigung im Spiel (nur ALLOWED_USER_IDS)
  /backup    - Manuelles Backup der Weltdaten (nur ALLOWED_USER_IDS)
  /backups   - Backups ansehen/löschen (nur ADMIN_USER_IDS)
  /autorestart - Geplanten Neustart verwalten (nur ADMIN_USER_IDS)
  /update    - Prüft automatisch auf neue Version, lädt sie herunter und
               installiert sie: Stop, Backup, Update, Start - worlds/Configs
               bleiben erhalten (nur ADMIN_USER_IDS)
  /reinstall - Spielt die installierte Version neu ein (Backup vorher, Welten/Configs
               bleiben erhalten; nur ADMIN_USER_IDS)
  /restore   - Stellt ein Backup wieder her, überschreibt worlds/Configs
               (nur ADMIN_USER_IDS)
  /playtime  - Spielzeit eines Spielers anzeigen: /playtime <Name>
  /top       - Top-5-Rangliste nach Spielzeit
  /settings  - Einstellungen anzeigen/ändern (Ändern nur ADMIN_USER_IDS)
  /error     - Fehler/Warnungen aus dem Server-Log seit dem letzten Start
  /help      - Hilfe

Hintergrund-Tasks:
  - Offline/Online-Erkennung des Servers
  - Live-Log-Watcher: Join/Leave (mit Klarnamen + Spielzeit-Tracking) und
    Sammeln von Fehlern/Warnungen (Zusammenfassung beim Serverstart)
  - Automatisches Backup zu fester Uhrzeit mit Tages-/Wochen-Aufbewahrung
  - Geplanter täglicher Neustart
  (Zeiten und Anzahlen sind per /settings änderbar)

Konfiguration erfolgt über Umgebungsvariablen (siehe config.example.env).
"""

import asyncio
import glob
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# --- Konfiguration laden -----------------------------------------------
load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
SERVER_HOST = os.environ.get("SERVER_HOST", "127.0.0.1")
SERVER_PORT = int(os.environ.get("SERVER_PORT", "19132"))
# Zusätzlicher Erreichbarkeits-Check (TCP) für /status. Seit NetherNet (Bedrock 1.26.51+)
# läuft der Verbindungsaufbau über TCP auf SERVER_PORT. Nur Anzeige, löst keinen Alarm aus.
STATUS_TCP_CHECK = os.environ.get("STATUS_TCP_CHECK", "true").lower() in ("1", "true", "yes")
SERVICE_NAME = os.environ.get("SERVICE_NAME", "minecraft-bedrock")
SERVER_DIR = os.environ.get("SERVER_DIR", "/opt/bedrock")
GROUP_CHAT_ID = int(os.environ["GROUP_CHAT_ID"])
ALLOWED_USER_IDS = {
    int(uid.strip())
    for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",")
    if uid.strip()
}
# Höhere Berechtigungsstufe: nur diese dürfen /update und /restore nutzen.
# Fällt standardmäßig auf ALLOWED_USER_IDS zurück, wenn nicht separat gesetzt.
ADMIN_USER_IDS = {
    int(uid.strip())
    for uid in os.environ.get("ADMIN_USER_IDS", "").split(",")
    if uid.strip()
} or set(ALLOWED_USER_IDS)

COMMAND_FIFO_PATH = os.environ.get("COMMAND_FIFO_PATH", "/opt/bedrock/command_input")
RESTART_ANNOUNCE_THRESHOLDS = [30, 10, 5, 4, 3, 2, 1]

LOG_MODE = os.environ.get("LOG_MODE", "journalctl")
LOG_FILE_PATH = os.environ.get("LOG_FILE_PATH", "")

# Backups
BACKUP_DIR = os.environ.get("BACKUP_DIR", "/opt/bedrock-backups")

# Statistik-Datenbank (Spielzeiten)
# Standard: stats.db im selben Ordner wie bot.py (unabhängig vom Installationspfad)
STATS_DB_PATH = os.environ.get(
    "STATS_DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "stats.db")
)

# Updates
# UPDATE_DIR dient jetzt als Zwischenspeicher für automatisch heruntergeladene
# Versionen (siehe /update). Manuelles Ablegen von .zip-Dateien dort geht
# weiterhin, falls du eine bestimmte Version statt "latest" installieren willst.
UPDATE_DIR = os.environ.get("UPDATE_DIR", "/opt/bedrock-updates")
# Bewusst NICHT geschützt: behavior_packs, resource_packs, valid_known_packs.json.
# Dort liegen die Spielinhalte (Biome, Features, Rezepte, Items) der Standard-Packs,
# die zur Serverversion passen müssen. Bleiben sie alt, fehlen neue Inhalte und der
# Server meldet beim Start hunderte Registry-Fehler. Eigene Packs sind sicher: die
# ZIP enthält sie nicht, und das Update überschreibt nur Dateien, löscht aber keine.
UPDATE_PROTECTED_PATHS = [
    p.strip()
    for p in os.environ.get(
        "UPDATE_PROTECTED_PATHS",
        "worlds,permissions.json,allowlist.json,server.properties",
    ).split(",")
    if p.strip()
]
# Offizielle (inoffiziell dokumentierte) Minecraft-API, die auch der
# eingebaute Update-Checker von Mojang nutzt. Braucht einen bestimmten
# User-Agent, da die CDN sonst Skript-Zugriffe blockiert.
BEDROCK_DOWNLOAD_API = "https://net-secondary.web.minecraft-services.net/api/v1.0/download/links"
BEDROCK_DOWNLOAD_USER_AGENT = (
    "Mozilla/4.0 (compatible; MSIE 6.0; Windows NT 5.1; BEDROCK-UPDATER)"
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bedrock-bot")
# httpx loggt sonst jede Telegram-Request-URL inkl. Bot-Token ins Journal
logging.getLogger("httpx").setLevel(logging.WARNING)

server_was_online: bool | None = None
maintenance_mode = False  # True während geplantem Stop/Restart/Update/Restore
active_sessions: dict[str, float] = {}  # Name -> Unix-Timestamp (Join)
last_error_line: str | None = None
server_version: str | None = None  # aus der Startzeile im Server-Log
server_booting = False  # True zwischen "Version:"-Zeile und "Server started."
# Seit dem letzten Serverstart gesammelte Log-Meldungen (für /error)
# Gleichartige Meldungen werden zu einer Gruppe zusammengefasst:
#   Vorlage -> {"count": Anzahl, "example": erste Originalzeile}
log_issues: dict[str, dict[str, dict]] = {"error": {}, "warn": {}}
MAX_ISSUE_GROUPS = 300
log_issue_counts: dict[str, int] = {"error": 0, "warn": 0}
startup_issue_counts: dict[str, int] = {"error": 0, "warn": 0}

JOIN_LEAVE_RE = re.compile(r"Player (connected|disconnected):\s*([^,]+),")
VERSION_RE = re.compile(r"INFO\] Version:\s*(\d+(?:\.\d+)+)")
SERVER_STARTED_RE = re.compile(r"INFO\] Server started\.")
ERROR_RE = re.compile(r"\b(ERROR|Exception|FATAL)\b", re.IGNORECASE)
LOG_LEVEL_RE = re.compile(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}:\d{3} (INFO|WARN|ERROR)\]")
ERROR_TIMESTAMP_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}:\d{3} ")


# --- Datenbank (Spielzeiten) ------------------------------------------------
def init_db():
    os.makedirs(os.path.dirname(STATS_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(STATS_DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_name TEXT NOT NULL,
            join_time REAL NOT NULL,
            leave_time REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()


def _db_record_session(name: str, join_time: float, leave_time: float):
    conn = sqlite3.connect(STATS_DB_PATH)
    conn.execute(
        "INSERT INTO sessions (player_name, join_time, leave_time) VALUES (?, ?, ?)",
        (name, join_time, leave_time),
    )
    conn.commit()
    conn.close()


async def db_record_session(name: str, join_time: float, leave_time: float):
    await asyncio.get_event_loop().run_in_executor(
        None, _db_record_session, name, join_time, leave_time
    )


def _db_total_playtime(name: str) -> float:
    conn = sqlite3.connect(STATS_DB_PATH)
    cur = conn.execute(
        "SELECT COALESCE(SUM(leave_time - join_time), 0) FROM sessions "
        "WHERE player_name = ? COLLATE NOCASE",
        (name,),
    )
    total = cur.fetchone()[0]
    conn.close()
    return total


async def db_total_playtime(name: str) -> float:
    return await asyncio.get_event_loop().run_in_executor(None, _db_total_playtime, name)


def _db_top_players(limit: int = 5):
    conn = sqlite3.connect(STATS_DB_PATH)
    cur = conn.execute(
        "SELECT player_name, SUM(leave_time - join_time) as total "
        "FROM sessions GROUP BY player_name COLLATE NOCASE "
        "ORDER BY total DESC LIMIT ?",
        (limit,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


async def db_top_players(limit: int = 5):
    return await asyncio.get_event_loop().run_in_executor(None, _db_top_players, limit)


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, _ = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


# --- Fehler-/Warnungs-Sammlung (für /error) ---------------------------------
def classify_log_line(line: str) -> str | None:
    """'error', 'warn' oder None. Maßgeblich ist die Stufe der Logzeile; nur
    Zeilen ohne Stufe (z.B. Absturzmeldungen) werden am Text erkannt."""
    level = LOG_LEVEL_RE.search(line)
    if level:
        return {"ERROR": "error", "WARN": "warn"}.get(level.group(1))
    return "error" if ERROR_RE.search(line) else None


def reset_log_issues():
    for kind in ("error", "warn"):
        log_issues[kind].clear()
        log_issue_counts[kind] = 0
        startup_issue_counts[kind] = 0


def issue_template(line: str) -> str:
    """Macht aus einer Logzeile eine Vorlage, indem Zeitstempel und veränderliche
    Teile (Namen in Anführungszeichen, Zahlen) ersetzt werden. So landen z.B.
    hunderte Rezept-Fehler mit unterschiedlichem Rezeptnamen in einer Gruppe."""
    text = ERROR_TIMESTAMP_RE.sub("[", strip_log_prefix(line), count=1)
    text = re.sub(r'"[^"]*"', '"…"', text)
    text = re.sub(r"'[^']*'", "'…'", text)
    return re.sub(r"\b\d+\b", "#", text)


def record_log_issue(kind: str, line: str):
    template = issue_template(line)
    groups = log_issues[kind]
    if template not in groups and len(groups) >= MAX_ISSUE_GROUPS:
        template = "(weitere abweichende Meldungen)"
    group = groups.setdefault(template, {"count": 0, "example": ERROR_TIMESTAMP_RE.sub("[", strip_log_prefix(line), count=1)})
    group["count"] += 1
    log_issue_counts[kind] += 1
    if server_booting:
        startup_issue_counts[kind] += 1


def strip_log_prefix(line: str) -> str:
    """Kürzt Journal-Präfix (Datum/Host/Unit) und Zeitstempel für die Anzeige."""
    match = re.search(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}:\d{3} ", line)
    return line[match.start():] if match else line


# --- Server-Status (für /status und Offline-Erkennung) ---------------------
# Seit Bedrock 1.26.51 nutzt der Server NetherNet statt RakNet und beantwortet
# die frühere UDP-Abfrage (mcstatus) nicht mehr. Der Status setzt sich daher
# aus Quellen zusammen, die vom Netzwerkprotokoll unabhängig sind:
#   online   -> systemd-Dienst läuft
#   Spieler  -> Join/Leave-Zeilen im Server-Log (active_sessions)
#   Version  -> Startzeile "Version: x.y.z.w" im Server-Log
#   max      -> max-players aus server.properties
async def _service_is_active() -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "is-active", SERVICE_NAME,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        return out.decode().strip() == "active"
    except Exception as e:
        log.warning("systemctl is-active fehlgeschlagen: %s", e)
        return False


async def _tcp_port_open() -> bool:
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(SERVER_HOST, SERVER_PORT), timeout=3
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


def read_max_players() -> int | None:
    try:
        with open(os.path.join(SERVER_DIR, "server.properties"), encoding="utf-8") as f:
            for line in f:
                if line.startswith("max-players="):
                    return int(line.split("=", 1)[1].strip())
    except Exception as e:
        log.debug("max-players nicht lesbar: %s", e)
    return None


async def query_server():
    """Liefert den Serverstatus oder None, wenn der Dienst nicht läuft."""
    if not await _service_is_active():
        return None
    return {
        "online": True,
        "players_online": len(active_sessions),
        "players_max": read_max_players(),
        "version": server_version,
        "port_ok": (await _tcp_port_open()) if STATUS_TCP_CHECK else None,
    }


def versions_match(installed: str, latest: str) -> bool:
    """Vergleicht Versionsnummern mit unterschiedlich vielen Teilen
    (z.B. '1.26.61' mit '1.26.61.1'), indem auf die kürzere Länge gekürzt wird."""
    n = min(len(installed.split(".")), len(latest.split(".")))
    return installed.split(".")[:n] == latest.split(".")[:n]


# --- Befehle an die Server-Konsole senden (über FIFO) -----------------------
def _write_to_fifo(command: str):
    with open(COMMAND_FIFO_PATH, "w") as f:
        f.write(command + "\n")


async def send_server_command(command: str) -> bool:
    try:
        await asyncio.get_event_loop().run_in_executor(None, _write_to_fifo, command)
        return True
    except Exception as e:
        log.error("Konnte Befehl nicht an Server-FIFO senden: %s", e)
        return False


async def do_restart_service(notify):
    global maintenance_mode
    maintenance_mode = True
    try:
        subprocess.run(
            ["systemctl", "restart", SERVICE_NAME],
            check=True, capture_output=True, text=True, timeout=30,
        )
        await notify("✅ Neustart-Befehl gesendet.")
    except subprocess.CalledProcessError as e:
        log.error("Restart fehlgeschlagen: %s", e.stderr)
        await notify(f"❌ Neustart fehlgeschlagen:\n{e.stderr}")
    except Exception as e:
        log.error("Restart fehlgeschlagen: %s", e)
        await notify(f"❌ Neustart fehlgeschlagen: {e}")
    finally:
        maintenance_mode = False


async def perform_countdown(seconds: int, notify, action_label: str) -> bool:
    """Kündigt eine Aktion (Neustart/Update) im Spiel an, mit Countdown bei
    festen Schwellwerten. Gibt True zurück, wenn tatsächlich gewartet wurde
    (Spieler waren online), False wenn sofort fortgefahren werden konnte."""
    # Für Telegram in Backticks gesetzt (verhindert Auto-Links bei
    # Versionsnummern/Dateinamen mit Punkten), fürs Spiel unverändert.
    label_md = f"`{action_label}`"

    info = await query_server()
    if info is None or info["players_online"] == 0:
        await notify(f"🔄 Niemand online - {label_md} kann sofort starten ...")
        return False

    await notify(
        f"⏳ Countdown gestartet: {label_md} in {seconds}s "
        f"({info['players_online']} Spieler online werden im Spiel informiert)."
    )

    ok = await send_server_command(f"say §e[Bot] {action_label} in {seconds} Sekunden!")
    if not ok:
        await notify(
            "⚠️ Konnte keine Ankündigung ins Spiel schicken (FIFO nicht erreichbar) - "
            "fahre trotzdem mit dem Countdown fort."
        )

    thresholds = sorted({t for t in RESTART_ANNOUNCE_THRESHOLDS if t < seconds}, reverse=True)
    elapsed = 0
    for t in thresholds:
        await asyncio.sleep(seconds - t - elapsed)
        elapsed = seconds - t
        await send_server_command(f"say §e[Bot] {action_label} in {t} Sekunden!")

    remaining = seconds - elapsed
    if remaining > 0:
        await asyncio.sleep(remaining)

    await send_server_command(f"say §c[Bot] {action_label} startet jetzt!")
    await asyncio.sleep(1)
    return True


async def perform_restart_sequence(seconds: int, notify):
    """Führt den Countdown + Neustart aus. `notify` ist eine async Funktion,
    die einen Text entgegennimmt (z.B. update.message.reply_text oder eine
    Funktion, die an die Telegram-Gruppe sendet)."""
    await perform_countdown(seconds, notify, "Server-Neustart")
    await do_restart_service(notify)


# --- Einstellungen zur Laufzeit (/settings) ---------------------------------
# Admins ändern diese Werte im Chat; sie werden in der Datenbank gespeichert und
# überstehen Neustarts. Ohne gespeicherten Wert gilt der Standard, der aus der
# .env kommt (siehe config.example.env). Pfade, Token und Benutzer-IDs gehören
# bewusst NICHT hierher, die bleiben in der .env.
def _parse_bool(raw) -> bool:
    value = str(raw).strip().lower()
    if value in ("an", "ein", "ja", "on", "true", "1", "yes"):
        return True
    if value in ("aus", "nein", "off", "false", "0", "no"):
        return False
    raise ValueError("bitte 'an' oder 'aus' angeben")


def _parse_time(raw) -> str:
    match = re.fullmatch(r"\s*(\d{1,2})[:.](\d{2})\s*", str(raw))
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        raise ValueError("bitte als HH:MM angeben, z. B. 03:30")
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def _int_parser(low: int, high: int):
    def parse(raw) -> int:
        try:
            number = int(str(raw).strip())
        except ValueError:
            raise ValueError("bitte eine ganze Zahl angeben")
        if not low <= number <= high:
            raise ValueError(f"erlaubt sind {low} bis {high}")
        return number
    return parse


def _parse_timezone(raw) -> str:
    name = str(raw).strip()
    try:
        ZoneInfo(name)
    except Exception:
        raise ValueError("unbekannte Zeitzone, z. B. Europe/Berlin")
    return name


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


_LEGACY_RESTART_TIME = "{:02d}:{:02d}".format(
    int(_env("SCHEDULED_RESTART_HOUR", "4")), int(_env("SCHEDULED_RESTART_MINUTE", "0"))
)

# Schlüssel -> Beschreibung. "default" kommt aus der .env (bzw. fest), "parse"
# prüft Eingaben aus dem Chat und liefert den typisierten Wert.
SETTINGS: dict[str, dict] = {
    "backup_aktiv": dict(
        group="Backup", parse=_parse_bool,
        default=_parse_bool(_env("BACKUP_ENABLED", "an")),
        desc="Automatisches Backup an/aus",
    ),
    "backup_zeit": dict(
        group="Backup", parse=_parse_time,
        default=_parse_time(_env("BACKUP_TIME", "03:30")),
        desc="Uhrzeit des täglichen Backups (HH:MM)",
    ),
    "backup_tage": dict(
        group="Backup", parse=_int_parser(1, 60),
        default=_int_parser(1, 60)(_env("BACKUP_DAILY_KEEP", _env("BACKUP_RETENTION_COUNT", "7"))),
        desc="Tages-Backups, die aufbewahrt werden (das neueste je Tag)",
    ),
    "backup_wochen": dict(
        group="Backup", parse=_int_parser(0, 52),
        default=_int_parser(0, 52)(_env("BACKUP_WEEKLY_KEEP", "4")),
        desc="Zusätzliche Wochen-Backups (je Woche das neueste, älter als die Tages-Backups)",
    ),
    "backup_sicherheit": dict(
        group="Backup", parse=_int_parser(1, 30),
        default=_int_parser(1, 30)(_env("BACKUP_SAFETY_KEEP", "3")),
        desc="Sicherungen vor Update/Neuinstallation/Restore, die aufbewahrt werden",
    ),
    "neustart_aktiv": dict(
        group="Geplanter Neustart", parse=_parse_bool,
        default=_parse_bool(_env("SCHEDULED_RESTART_ENABLED", "aus")),
        desc="Täglicher Neustart an/aus",
    ),
    "neustart_zeit": dict(
        group="Geplanter Neustart", parse=_parse_time,
        default=_parse_time(_env("SCHEDULED_RESTART_TIME", _LEGACY_RESTART_TIME)),
        desc="Uhrzeit des täglichen Neustarts (HH:MM)",
    ),
    "neustart_countdown": dict(
        group="Geplanter Neustart", parse=_int_parser(0, 600),
        default=_int_parser(0, 600)(_env(
            "SCHEDULED_RESTART_COUNTDOWN_SECONDS", _env("RESTART_COUNTDOWN_SECONDS", "60"))),
        desc="Countdown vor dem geplanten Neustart in Sekunden",
    ),
    "neustart_ueberspringen": dict(
        group="Geplanter Neustart", parse=_parse_bool,
        default=_parse_bool(_env("SCHEDULED_RESTART_SKIP_IF_EMPTY", "an")),
        desc="Geplanten Neustart überspringen, wenn niemand online ist",
    ),
    "countdown": dict(
        group="Allgemein", parse=_int_parser(0, 600),
        default=_int_parser(0, 600)(_env("RESTART_COUNTDOWN_SECONDS", "60")),
        desc="Standard-Countdown für /restart, /update, /reinstall, /restore in Sekunden",
    ),
    "fehler_alarm": dict(
        group="Allgemein", parse=_parse_bool,
        default=_parse_bool(_env("ERROR_ALERTS_RUNTIME", "an")),
        desc="Fehler im laufenden Betrieb einzeln melden (Start-Fehler nie, siehe /error)",
    ),
    "pruef_intervall": dict(
        group="Allgemein", parse=_int_parser(10, 600),
        default=_int_parser(10, 600)(_env("POLL_INTERVAL", "30")),
        desc="Wie oft der Serverstatus geprüft wird, in Sekunden",
    ),
    "zeitzone": dict(
        group="Allgemein", parse=_parse_timezone,
        # Bewusst nicht geprüft: fehlen die Zeitzonen-Daten, soll der Bot trotzdem starten
        # (now_local() fällt dann auf die Systemzeit zurück)
        default=_env("BOT_TIMEZONE", "Europe/Berlin").strip(),
        desc="Zeitzone für Backup- und Neustartzeiten",
    ),
}

_setting_overrides: dict[str, object] = {}
# Je ein Event pro Hintergrund-Task, damit Änderungen sofort greifen
settings_events: dict[str, asyncio.Event] = {"backup": asyncio.Event(), "restart": asyncio.Event()}


def cfg(key: str):
    """Aktueller Wert einer Einstellung (gespeicherter Wert oder Standard)."""
    return _setting_overrides.get(key, SETTINGS[key]["default"])


def format_setting_value(key: str, value) -> str:
    return ("an" if value else "aus") if isinstance(value, bool) else str(value)


def _db_save_setting(key: str, value: str | None):
    conn = sqlite3.connect(STATS_DB_PATH)
    if value is None:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    else:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


def load_settings():
    """Liest gespeicherte Einstellungen beim Start. Ungültige Werte werden ignoriert."""
    conn = sqlite3.connect(STATS_DB_PATH)
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    for key, raw in rows:
        if key not in SETTINGS:
            continue
        try:
            _setting_overrides[key] = SETTINGS[key]["parse"](raw)
        except ValueError as e:
            log.warning("Gespeicherte Einstellung %s=%r ungültig (%s) - nutze Standard", key, raw, e)


def wake_schedulers():
    for event in settings_events.values():
        event.set()


def set_setting(key: str, raw: str) -> tuple[object, object]:
    """Setzt und speichert eine Einstellung. Wirft ValueError bei ungültiger Eingabe.
    Gibt (alter_wert, neuer_wert) zurück."""
    value = SETTINGS[key]["parse"](raw)
    old = cfg(key)
    _db_save_setting(key, format_setting_value(key, value))
    _setting_overrides[key] = value
    wake_schedulers()
    return old, value


def reset_setting(key: str) -> tuple[object, object]:
    old = cfg(key)
    _db_save_setting(key, None)
    _setting_overrides.pop(key, None)
    wake_schedulers()
    return old, cfg(key)


_tz_warned = False


def now_local() -> datetime:
    """Aktuelle Zeit in der eingestellten Zeitzone (Fallback: Systemzeit)."""
    global _tz_warned
    try:
        return datetime.now(ZoneInfo(cfg("zeitzone")))
    except Exception as e:
        if not _tz_warned:
            _tz_warned = True
            log.warning("Zeitzone %s nicht nutzbar (%s) - nutze Systemzeit. "
                        "Abhilfe: 'apt install tzdata' oder 'pip install tzdata' im venv.",
                        cfg("zeitzone"), e)
        return datetime.now().astimezone()


def next_occurrence(hhmm: str) -> datetime:
    """Nächster Zeitpunkt, an dem die Uhr HH:MM zeigt (heute oder morgen)."""
    now = now_local()
    hour, minute = (int(part) for part in hhmm.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target = (now + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return target


def seconds_until(target: datetime) -> float:
    # Über UTC rechnen, damit Sommerzeit-Wechsel nicht um eine Stunde verfälschen
    return max(0.0, (target.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds())


async def wait_or_change(name: str, seconds: float) -> bool:
    """Wartet bis zu `seconds`. Gibt True zurück, wenn zwischendurch eine
    Einstellung geändert wurde (Zeitpunkt dann neu berechnen), sonst False."""
    event = settings_events[name]
    try:
        await asyncio.wait_for(event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        return False
    event.clear()
    return True


WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def format_when(moment: datetime) -> str:
    return f"{WEEKDAYS[moment.weekday()]} {moment:%d.%m. %H:%M}"


# --- Backup-Logik ------------------------------------------------------------
# Dateinamen: backup_<Datum>_<Uhrzeit>.tar.gz (automatisch/manuell) und
# backup_<Datum>_<Uhrzeit>_pre-update|pre-reinstall|pre-restore.tar.gz (Sicherungen
# vor einem Eingriff). Beide Arten werden getrennt aufbewahrt.
BACKUP_NAME_RE = re.compile(
    r"^backup_(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})(?:_(pre-[a-z]+))?\.tar\.gz$"
)
last_retention_summary = ""
skip_next_restart = False  # /autorestart skip: nächsten Lauf einmalig auslassen


def parse_backup_name(path: str):
    """(Zeitpunkt, Art) oder None, wenn der Name nicht zum Schema passt.
    Art ist None für normale Backups, sonst z.B. 'pre-update'."""
    match = BACKUP_NAME_RE.match(os.path.basename(path))
    if not match:
        return None
    return datetime.strptime(match.group(1), "%Y-%m-%d_%H-%M-%S"), match.group(2)


def classify_backups(items, daily: int, weekly: int, safety: int) -> dict[str, str]:
    """items: Liste von (pfad, zeitpunkt, art). Gibt {pfad: rolle} für alle Backups
    zurück, die aufbewahrt werden. Rollen: 'tag', 'woche', 'sicherung'.

    Normale Backups:
      - je eines der `daily` jüngsten Tage mit Backups (das neueste des Tages)
      - zusätzlich je eines der `weekly` jüngsten Kalenderwochen unter den übrigen
        (älter als die Tages-Backups), jeweils das neueste der Woche
    Sicherungen (art beginnt mit 'pre-'): die `safety` neuesten."""
    roles: dict[str, str] = {}

    regular = sorted((i for i in items if i[2] is None), key=lambda i: i[1], reverse=True)
    newest_per_day: dict = {}
    for path, moment, _ in regular:
        newest_per_day.setdefault(moment.date(), path)
    daily_days = sorted(newest_per_day, reverse=True)[:daily]
    for day in daily_days:
        roles[newest_per_day[day]] = "tag"

    newest_per_week: dict = {}
    for path, moment, _ in regular:
        if moment.date() in daily_days:
            continue
        iso = moment.isocalendar()
        newest_per_week.setdefault((iso[0], iso[1]), path)
    for week in sorted(newest_per_week, reverse=True)[:weekly]:
        roles[newest_per_week[week]] = "woche"

    safeties = sorted((i for i in items if i[2] is not None), key=lambda i: i[1], reverse=True)
    for path, _, _ in safeties[:safety]:
        roles[path] = "sicherung"
    return roles


def select_backups_to_keep(items, daily: int, weekly: int, safety: int) -> set[str]:
    return set(classify_backups(items, daily, weekly, safety))


def read_backup_items() -> list[tuple[str, datetime, str | None]]:
    items = []
    for path in glob.glob(os.path.join(BACKUP_DIR, "backup_*.tar.gz")):
        parsed = parse_backup_name(path)
        if parsed:
            items.append((path, parsed[0], parsed[1]))
    return items


def apply_backup_retention() -> list[str]:
    """Löscht Backups, die nach den Einstellungen nicht mehr aufbewahrt werden.
    Dateien mit unbekanntem Namen werden nie angefasst. Gibt die gelöschten Namen zurück."""
    global last_retention_summary
    items = read_backup_items()

    keep = select_backups_to_keep(
        items, cfg("backup_tage"), cfg("backup_wochen"), cfg("backup_sicherheit")
    )
    removed = []
    for path, _, _ in items:
        if path in keep:
            continue
        try:
            os.remove(path)
            removed.append(os.path.basename(path))
            log.info("Altes Backup gelöscht: %s", path)
        except OSError as e:
            log.error("Konnte altes Backup nicht löschen: %s", e)

    regular_kept = sum(1 for path, _, kind in items if path in keep and kind is None)
    safety_kept = sum(1 for path, _, kind in items if path in keep and kind is not None)
    last_retention_summary = (
        f"Aufbewahrt: {regular_kept} Backups, {safety_kept} Sicherungen"
        + (f"; {len(removed)} alte gelöscht" if removed else "")
    )
    return removed


def _do_backup(kind: str = "") -> tuple[str, int]:
    """Erstellt ein tar.gz-Backup von SERVER_DIR. kind ist leer oder z.B.
    'pre-update'. Gibt (pfad, größe_bytes) zurück."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = now_local().strftime("%Y-%m-%d_%H-%M-%S")
    archive_path_base = os.path.join(BACKUP_DIR, f"backup_{timestamp}" + (f"_{kind}" if kind else ""))
    archive_path = shutil.make_archive(archive_path_base, "gztar", SERVER_DIR)
    size = os.path.getsize(archive_path)
    apply_backup_retention()
    return archive_path, size


async def run_backup(kind: str = "") -> tuple[str, int]:
    return await asyncio.get_event_loop().run_in_executor(None, _do_backup, kind)


# --- Automatischer Download der neuesten Version -----------------------
def _fetch_latest_linux_info() -> tuple[str, str]:
    """Fragt die offizielle Minecraft-API nach der aktuellen Linux-Server-
    Version. Gibt (download_url, version_string) zurück, z.B.
    ('https://www.minecraft.net/.../bedrock-server-1.21.90.4.zip', '1.21.90.4')."""
    req = urllib.request.Request(
        BEDROCK_DOWNLOAD_API, headers={"User-Agent": BEDROCK_DOWNLOAD_USER_AGENT}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.load(resp)

    for link in data["result"]["links"]:
        if link["downloadType"] == "serverBedrockLinux":
            url = link["downloadUrl"]
            match = re.search(r"bedrock-server-([\d.]+)\.zip", url)
            version = match.group(1) if match else "unbekannt"
            return url, version

    raise RuntimeError("serverBedrockLinux nicht in der API-Antwort gefunden.")


async def fetch_latest_linux_info() -> tuple[str, str]:
    return await asyncio.get_event_loop().run_in_executor(None, _fetch_latest_linux_info)


def _download_file(url: str, dest_path: str):
    req = urllib.request.Request(url, headers={"User-Agent": BEDROCK_DOWNLOAD_USER_AGENT})
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    # Erst in eine .part-Datei laden und am Ende umbenennen: ein abgebrochener
    # Download hinterlässt sonst eine kaputte ZIP, die beim nächsten Mal
    # als "schon vorhanden" wiederverwendet würde.
    part_path = dest_path + ".part"
    try:
        with urllib.request.urlopen(req, timeout=180) as resp, open(part_path, "wb") as f:
            shutil.copyfileobj(resp, f)
        os.replace(part_path, dest_path)
    finally:
        if os.path.exists(part_path):
            os.remove(part_path)


async def download_file(url: str, dest_path: str):
    await asyncio.get_event_loop().run_in_executor(None, _download_file, url, dest_path)


# --- Update-Logik --------------------------------------------------------
def _validate_server_zip(zip_path: str):
    """Prüft vor dem Stoppen des Servers, ob die ZIP vollständig und ein
    Bedrock-Server-Archiv ist. Wirft eine Exception, wenn nicht."""
    if not zipfile.is_zipfile(zip_path):
        raise RuntimeError("keine gültige ZIP-Datei")
    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        if bad is not None:
            raise RuntimeError(f"beschädigte Datei im Archiv: {bad}")
        if "bedrock_server" not in zf.namelist():
            raise RuntimeError("enthält keine Datei 'bedrock_server'")


async def validate_server_zip(zip_path: str):
    await asyncio.get_event_loop().run_in_executor(None, _validate_server_zip, zip_path)


def _verify_backup_has_worlds(backup_path: str):
    """Stellt sicher, dass das Backup lesbar ist und (falls vorhanden) worlds/
    enthält. Wirft eine Exception, wenn nicht."""
    if os.path.getsize(backup_path) == 0:
        raise RuntimeError("Backup-Datei ist leer")
    if not os.path.isdir(os.path.join(SERVER_DIR, "worlds")):
        return  # nichts zu prüfen
    with tarfile.open(backup_path, "r:gz") as tf:
        for name in tf.getnames():
            if name.lstrip("./").startswith("worlds/"):
                return
    raise RuntimeError("worlds/ fehlt im Backup")


async def verify_backup_has_worlds(backup_path: str):
    await asyncio.get_event_loop().run_in_executor(None, _verify_backup_has_worlds, backup_path)


def cleanup_old_update_zips(keep_path: str) -> int:
    """Löscht alle heruntergeladenen Server-ZIPs (und angebrochene Downloads)
    in UPDATE_DIR außer keep_path. Gibt die Anzahl gelöschter Dateien zurück."""
    removed = 0
    keep = os.path.abspath(keep_path)
    for pattern in ("bedrock-server-*.zip", "bedrock-server-*.zip.part"):
        for path in glob.glob(os.path.join(UPDATE_DIR, pattern)):
            if os.path.abspath(path) == keep:
                continue
            try:
                os.remove(path)
                removed += 1
                log.info("Alte Update-Datei gelöscht: %s", path)
            except OSError as e:
                log.error("Konnte %s nicht löschen: %s", path, e)
    return removed


def _do_extract_update(zip_path: str):
    """Entpackt zip_path und kopiert alles außer UPDATE_PROTECTED_PATHS
    nach SERVER_DIR. Läuft blockierend, daher im Executor aufrufen."""
    tmp_dir = tempfile.mkdtemp(prefix="bedrock-update-")
    extract_dir = os.path.join(tmp_dir, "extracted")
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extract_dir)

        for p in UPDATE_PROTECTED_PATHS:
            full = os.path.join(extract_dir, p)
            if os.path.isdir(full):
                shutil.rmtree(full)
            elif os.path.isfile(full):
                os.remove(full)

        for root, _dirs, files in os.walk(extract_dir):
            rel = os.path.relpath(root, extract_dir)
            target_root = SERVER_DIR if rel == "." else os.path.join(SERVER_DIR, rel)
            os.makedirs(target_root, exist_ok=True)
            for fname in files:
                shutil.copy2(os.path.join(root, fname), os.path.join(target_root, fname))

        bedrock_bin = os.path.join(SERVER_DIR, "bedrock_server")
        if os.path.isfile(bedrock_bin):
            os.chmod(bedrock_bin, 0o755)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def run_extract_update(zip_path: str):
    await asyncio.get_event_loop().run_in_executor(None, _do_extract_update, zip_path)


async def do_stop_service(notify) -> bool:
    global maintenance_mode
    maintenance_mode = True
    try:
        subprocess.run(
            ["systemctl", "stop", SERVICE_NAME],
            check=True, capture_output=True, text=True, timeout=30,
        )
        return True
    except Exception as e:
        log.error("Stoppen fehlgeschlagen: %s", e)
        await notify(f"❌ Server konnte nicht gestoppt werden: {e}")
        maintenance_mode = False
        return False


async def do_start_service(notify) -> bool:
    global maintenance_mode
    try:
        subprocess.run(
            ["systemctl", "start", SERVICE_NAME],
            check=True, capture_output=True, text=True, timeout=30,
        )
        return True
    except Exception as e:
        log.error("Starten fehlgeschlagen: %s", e)
        await notify(f"❌ Server konnte nicht gestartet werden: {e}")
        return False
    finally:
        maintenance_mode = False


async def perform_update_sequence(zip_path: str, seconds: int, notify, reinstall: bool = False):
    """Update (neue Version) oder Neuinstallation (gleiche Version) aus zip_path.
    Ablauf: ZIP prüfen, Countdown, Server stoppen, Backup (inkl. worlds/) anlegen
    und prüfen, entpacken (geschützte Pfade bleiben), Server starten, alte ZIPs löschen."""
    global maintenance_mode
    zip_name = os.path.basename(zip_path)
    word = "Neuinstallation" if reinstall else "Update"
    before = "vor der Neuinstallation" if reinstall else "vor dem Update"

    # Vor dem Stoppen prüfen: eine kaputte ZIP darf den Server nie lahmlegen
    try:
        await validate_server_zip(zip_path)
    except Exception as e:
        log.error("ZIP-Prüfung fehlgeschlagen (%s): %s", zip_name, e)
        try:
            os.remove(zip_path)  # defekte Datei nicht wiederverwenden
        except OSError:
            pass
        await notify(
            f"❌ `{zip_name}` ist unbrauchbar ({e}) und wurde gelöscht. "
            f"Server wurde nicht angefasst - bitte {word} erneut starten."
        )
        return

    await perform_countdown(seconds, notify, f"Server-{word} ({zip_name})")

    await notify("🛑 Stoppe Server ...")
    if not await do_stop_service(notify):
        return

    had_worlds = os.path.isdir(os.path.join(SERVER_DIR, "worlds"))

    await notify(f"💾 Erstelle Backup {before} ...")
    try:
        backup_path, backup_size = await run_backup("pre-reinstall" if reinstall else "pre-update")
        await verify_backup_has_worlds(backup_path)
        await notify(
            f"✅ Backup fertig und geprüft: `{os.path.basename(backup_path)}` "
            f"({backup_size / (1024*1024):.1f} MB)",
        )
    except Exception as e:
        log.error("Backup vor %s fehlgeschlagen: %s", word, e)
        maintenance_mode = False
        await notify(
            f"❌ Backup fehlgeschlagen ({e}) - {word} wird abgebrochen, "
            "Server bleibt aus Sicherheitsgründen gestoppt. Bitte manuell prüfen."
        )
        return

    protected = ", ".join(f"`{p}`" for p in UPDATE_PROTECTED_PATHS)
    await notify(f"📦 Spiele die Dateien aus der ZIP ein - unangetastet bleiben: {protected} ...")
    try:
        await run_extract_update(zip_path)
        if had_worlds and not os.path.isdir(os.path.join(SERVER_DIR, "worlds")):
            raise RuntimeError("worlds/ ist nach dem Entpacken nicht mehr vorhanden")
    except Exception as e:
        log.error("%s-Entpacken fehlgeschlagen: %s", word, e)
        maintenance_mode = False
        await notify(
            f"❌ {word} fehlgeschlagen beim Entpacken ({e}). "
            "Server bleibt gestoppt - Backup ist vorhanden, bitte manuell prüfen."
        )
        return

    await notify("🚀 Starte Server ...")
    if not await do_start_service(notify):
        return

    removed = cleanup_old_update_zips(zip_path)
    cleanup_note = f" {removed} alte Update-Datei(en) gelöscht." if removed else ""
    await notify(f"✅ {word} abgeschlossen und Server läuft wieder.{cleanup_note}")


# --- Restore-Logik -----------------------------------------------------------
def _do_restore_extract(backup_path: str):
    """Entpackt ein Backup-Archiv direkt über SERVER_DIR (überschreibt alles,
    inkl. worlds/ - das ist bei /restore so gewollt)."""
    with tarfile.open(backup_path, "r:gz") as tf:
        tf.extractall(SERVER_DIR)

    bedrock_bin = os.path.join(SERVER_DIR, "bedrock_server")
    if os.path.isfile(bedrock_bin):
        os.chmod(bedrock_bin, 0o755)


async def run_restore_extract(backup_path: str):
    await asyncio.get_event_loop().run_in_executor(None, _do_restore_extract, backup_path)


def list_backups() -> list[str]:
    return sorted(glob.glob(os.path.join(BACKUP_DIR, "backup_*.tar.gz")), reverse=True)


async def perform_restore_sequence(backup_path: str, seconds: int, notify):
    global maintenance_mode
    backup_name = os.path.basename(backup_path)

    await perform_countdown(seconds, notify, f"Wiederherstellung ({backup_name})")

    await notify("🛑 Stoppe Server ...")
    if not await do_stop_service(notify):
        return

    await notify("💾 Sichere aktuellen Stand vor der Wiederherstellung (Sicherheitsnetz) ...")
    try:
        safety_path, safety_size = await run_backup("pre-restore")
        await notify(
            f"✅ Sicherheitsbackup: `{os.path.basename(safety_path)}` "
            f"({safety_size / (1024*1024):.1f} MB)"
        )
    except Exception as e:
        log.error("Sicherheitsbackup vor Restore fehlgeschlagen: %s", e)
        maintenance_mode = False
        await notify(
            f"❌ Sicherheitsbackup fehlgeschlagen ({e}) - Wiederherstellung wird "
            "abgebrochen, Server bleibt gestoppt. Bitte manuell prüfen."
        )
        return

    await notify(f"♻️ Stelle Backup wieder her: `{backup_name}` ...")
    try:
        await run_restore_extract(backup_path)
    except Exception as e:
        log.error("Restore-Entpacken fehlgeschlagen: %s", e)
        maintenance_mode = False
        await notify(
            f"❌ Wiederherstellung fehlgeschlagen ({e}). Server bleibt gestoppt - "
            "Sicherheitsbackup ist vorhanden, bitte manuell prüfen."
        )
        return

    await notify("🚀 Starte Server ...")
    if not await do_start_service(notify):
        return

    await notify("✅ Wiederherstellung abgeschlossen und Server läuft wieder.")


# --- Befehle ---------------------------------------------------------------
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    info = await query_server()
    if info is None:
        await update.message.reply_text("🔴 Server ist offline (Dienst läuft nicht).")
        return
    players_max = info["players_max"] if info["players_max"] is not None else "?"
    version = info["version"] or "unbekannt"
    text = (
        f"🟢 *Server online*\n"
        f"Spieler: {info['players_online']}/{players_max}\n"
        f"Version: `{version}`"
    )
    if info["port_ok"] is False:
        text += (
            f"\n⚠️ Dienst läuft, aber TCP-Port {SERVER_PORT} antwortet nicht - "
            f"Spieler können evtl. nicht verbinden."
        )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


def startup_summary_text() -> str:
    e, w = startup_issue_counts["error"], startup_issue_counts["warn"]
    head = f"🚀 Server gestartet (Version `{server_version or 'unbekannt'}`)"
    if e == 0 and w == 0:
        return head + " - keine Fehler oder Warnungen."
    parts = []
    if e:
        parts.append(f"{e} Fehler")
    if w:
        parts.append(f"{w} Warnung" if w == 1 else f"{w} Warnungen")
    return head + f"\n⚠️ Beim Start: {', '.join(parts)} - Details mit /error"


async def cmd_error(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/error [warn] [anzahl] - gesammelte Fehler (oder Warnungen) seit dem letzten Serverstart."""
    kind, limit = "error", 15
    for arg in context.args or []:
        if arg.lower() in ("warn", "warnings", "warnung", "warnungen", "w"):
            kind = "warn"
        elif arg.isdigit():
            limit = max(1, min(int(arg), 50))

    label = "Fehler" if kind == "error" else "Warnungen"
    total = log_issue_counts[kind]
    other = log_issue_counts["warn" if kind == "error" else "error"]
    other_label = "Warnungen" if kind == "error" else "Fehler"
    hint = "/error warn" if kind == "error" else "/error"

    if total == 0:
        await update.message.reply_text(
            f"✅ Keine {label} seit dem letzten Serverstart."
            + (f"\n({other} {other_label}: {hint})" if other else "")
        )
        return

    # Häufigste Gruppen zuerst
    groups = sorted(log_issues[kind].items(), key=lambda kv: kv[1]["count"], reverse=True)
    shown = groups[:limit]

    lines = [f"{'🔴' if kind == 'error' else '🟡'} {label} seit dem letzten Serverstart: {total} "
             f"({len(groups)} verschiedene Meldungen)"]
    for template, group in shown:
        if group["count"] > 1:
            lines.append(f"• {template[:200]}  (x{group['count']})")
            lines.append(f"   z.B. {group['example'][:200]}")
        else:
            lines.append("• " + group["example"][:220])
    if len(groups) > len(shown):
        lines.append(f"... und {len(groups) - len(shown)} weitere Meldungen (/error {min(limit + 15, 50)})")
    if other:
        lines.append(f"\n{other} {other_label}: {hint}")

    out = "\n".join(lines)
    if len(out) > 4000:
        out = out[:3990] + "\n..."
    await update.message.reply_text(out)  # bewusst ohne Markdown (Unterstriche in IDs)


# --- /settings, /backups, /autorestart (Verwaltung) ---------------------------
def settings_overview_text() -> str:
    lines = [f"⚙️ Einstellungen (Zeitzone: {cfg('zeitzone')})"]
    group = None
    for key, spec in SETTINGS.items():
        if spec["group"] != group:
            group = spec["group"]
            lines.append(f"\n{group}")
        value = format_setting_value(key, cfg(key))
        changed = " (geändert)" if key in _setting_overrides else ""
        lines.append(f"• {key} = {value}{changed}\n   {spec['desc']}")
    lines.append(
        "\nÄndern: /settings <name> <wert>, z. B. /settings backup_zeit 04:00\n"
        "Zurücksetzen: /settings reset <name>\n"
        "Backups verwalten: /backups, Neustart verwalten: /autorestart"
    )
    return "\n".join(lines)


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    is_admin = user_id in ADMIN_USER_IDS
    if not is_admin and user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Du bist nicht berechtigt, die Einstellungen zu sehen.")
        return

    args = context.args or []
    if not args:
        await update.message.reply_text(settings_overview_text())
        return
    if not is_admin:
        await update.message.reply_text("⛔ Nur Admins dürfen Einstellungen ändern.")
        return

    if args[0].lower() == "reset":
        key = args[1].lower() if len(args) > 1 else ""
        if key not in SETTINGS:
            await update.message.reply_text("Nutzung: /settings reset <name>  (Namen siehe /settings)")
            return
        old, new = reset_setting(key)
        await update.message.reply_text(
            f"↩️ {key}: {format_setting_value(key, old)} → {format_setting_value(key, new)} (Standard)"
        )
        log.info("Einstellung %s zurückgesetzt von user_id=%s", key, user_id)
        return

    key = args[0].lower()
    if key not in SETTINGS:
        await update.message.reply_text(f"❌ Unbekannte Einstellung '{args[0]}'. Alle Namen: /settings")
        return
    if len(args) == 1:
        await update.message.reply_text(
            f"{key} = {format_setting_value(key, cfg(key))}\n{SETTINGS[key]['desc']}\n"
            f"Ändern: /settings {key} <wert>"
        )
        return

    try:
        old, new = set_setting(key, " ".join(args[1:]))
    except ValueError as e:
        await update.message.reply_text(f"❌ {key}: {e}")
        return
    log.info("Einstellung %s: %r -> %r (user_id=%s)", key, old, new, user_id)

    text = f"✅ {key}: {format_setting_value(key, old)} → {format_setting_value(key, new)}"
    if key in ("backup_zeit", "backup_aktiv", "zeitzone") and cfg("backup_aktiv"):
        text += f"\nNächstes Backup: {format_when(next_occurrence(cfg('backup_zeit')))}"
    if key in ("neustart_zeit", "neustart_aktiv", "zeitzone") and cfg("neustart_aktiv"):
        text += f"\nNächster geplanter Neustart: {format_when(next_occurrence(cfg('neustart_zeit')))}"
    if key in ("backup_tage", "backup_wochen", "backup_sicherheit"):
        text += "\nGilt ab dem nächsten Backup; vorhandene Backups werden erst dann aufgeräumt."
    await update.message.reply_text(text)


ROLE_LABELS = {"tag": "Tages-Backup", "woche": "Wochen-Backup", "sicherung": "Sicherung"}


async def cmd_backups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/backups - Liste mit Einordnung; /backups delete <datei> - ein Backup löschen."""
    if update.effective_user.id not in ADMIN_USER_IDS:
        await update.message.reply_text("⛔ Nur Admins dürfen Backups verwalten.")
        return

    args = context.args or []
    items = read_backup_items()

    if args and args[0].lower() in ("delete", "loeschen", "löschen"):
        name = args[1] if len(args) > 1 else ""
        path = os.path.join(BACKUP_DIR, os.path.basename(name))
        if not name or os.path.basename(name) != name or not parse_backup_name(name) or not os.path.isfile(path):
            await update.message.reply_text(
                "Nutzung: /backups delete <dateiname>  (Namen siehe /backups)"
            )
            return
        if len(items) <= 1:
            await update.message.reply_text("❌ Das ist das einzige Backup - ich lösche es nicht.")
            return
        try:
            os.remove(path)
        except OSError as e:
            await update.message.reply_text(f"❌ Konnte nicht löschen: {e}")
            return
        log.info("Backup %s gelöscht von user_id=%s", name, update.effective_user.id)
        await update.message.reply_text(f"🗑 Gelöscht: `{name}`", parse_mode=ParseMode.MARKDOWN)
        return

    if not items:
        await update.message.reply_text(f"Keine Backups in {BACKUP_DIR} gefunden.")
        return

    roles = classify_backups(items, cfg("backup_tage"), cfg("backup_wochen"), cfg("backup_sicherheit"))
    items.sort(key=lambda i: i[1], reverse=True)
    total_bytes = 0
    lines = []
    for path, moment, kind in items:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        total_bytes += size
        if path in roles:
            role = ROLE_LABELS[roles[path]] + (f" ({kind})" if kind else "")
        else:
            role = "wird beim nächsten Backup gelöscht"
        if len(lines) < 30:
            lines.append(
                f"• `{os.path.basename(path)}`\n   {format_when(moment)} · {size / (1024*1024):.0f} MB · {role}"
            )
    header = (
        f"💾 {len(items)} Backups, zusammen {total_bytes / (1024**3):.1f} GB\n"
        f"Aufbewahrung: {cfg('backup_tage')} Tage, {cfg('backup_wochen')} Wochen, "
        f"{cfg('backup_sicherheit')} Sicherungen\n"
    )
    if cfg("backup_aktiv"):
        header += f"Nächstes Backup: {format_when(next_occurrence(cfg('backup_zeit')))}\n"
    else:
        header += "Automatisches Backup: aus\n"
    footer = "\n\nLöschen: /backups delete <dateiname>\nEinstellungen: /settings"
    if len(items) > len(lines):
        footer = f"\n... und {len(items) - len(lines)} weitere" + footer
    await update.message.reply_text(
        header + "\n" + "\n".join(lines) + footer, parse_mode=ParseMode.MARKDOWN
    )


def autorestart_status_text() -> str:
    if cfg("neustart_aktiv"):
        text = (
            f"🕓 Geplanter Neustart: an, täglich um {cfg('neustart_zeit')} ({cfg('zeitzone')})\n"
            f"Nächster: {format_when(next_occurrence(cfg('neustart_zeit')))}"
            + (" - wird einmalig übersprungen" if skip_next_restart else "")
        )
    else:
        text = "🕓 Geplanter Neustart: aus"
    text += (
        f"\nCountdown: {cfg('neustart_countdown')} s · überspringen, wenn niemand online: "
        f"{format_setting_value('', cfg('neustart_ueberspringen'))}"
    )
    text += (
        "\n\n/autorestart an | aus\n/autorestart zeit 04:00\n/autorestart countdown 60\n"
        "/autorestart skip  (nächsten Lauf einmalig auslassen, nochmal = zurücknehmen)"
    )
    return text


async def cmd_autorestart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global skip_next_restart
    if update.effective_user.id not in ADMIN_USER_IDS:
        await update.message.reply_text("⛔ Nur Admins dürfen den geplanten Neustart verwalten.")
        return

    args = [a.lower() for a in (context.args or [])]
    if not args:
        await update.message.reply_text(autorestart_status_text())
        return

    command = args[0]
    try:
        if command in ("an", "aus", "on", "off"):
            set_setting("neustart_aktiv", command)
        elif command == "zeit" and len(args) > 1:
            set_setting("neustart_zeit", args[1])
        elif command == "countdown" and len(args) > 1:
            set_setting("neustart_countdown", args[1])
        elif command in ("skip", "ueberspringen", "überspringen"):
            if not cfg("neustart_aktiv"):
                await update.message.reply_text("Der geplante Neustart ist aus - es gibt nichts zu überspringen.")
                return
            skip_next_restart = not skip_next_restart
        else:
            await update.message.reply_text(autorestart_status_text())
            return
    except ValueError as e:
        await update.message.reply_text(f"❌ {e}")
        return
    log.info("/autorestart %s von user_id=%s", " ".join(args), update.effective_user.id)
    await update.message.reply_text("✅ Gespeichert.\n\n" + autorestart_status_text())


async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Du bist nicht berechtigt, den Server neuzustarten.")
        log.warning("Unautorisierter Restart-Versuch von user_id=%s", user_id)
        return

    seconds = cfg("countdown")
    if context.args:
        try:
            seconds = max(0, int(context.args[0]))
        except ValueError:
            await update.message.reply_text(
                f"Ungültige Angabe, nutze Standard-Countdown ({cfg('countdown')}s)."
            )

    async def notify(text: str):
        await update.message.reply_text(text)

    await perform_restart_sequence(seconds, notify)


async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Du bist nicht berechtigt, ein Backup auszulösen.")
        return

    await update.message.reply_text("💾 Backup wird erstellt ...")
    try:
        path, size = await run_backup()
        size_mb = size / (1024 * 1024)
        await update.message.reply_text(
            f"✅ Backup fertig: `{os.path.basename(path)}` ({size_mb:.1f} MB)",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        log.error("Backup fehlgeschlagen: %s", e)
        await update.message.reply_text(f"❌ Backup fehlgeschlagen: {e}")


async def cmd_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_USER_IDS:
        await update.message.reply_text("⛔ Nur Admins dürfen ein Update auslösen.")
        return

    await update.message.reply_text("🔍 Prüfe aktuelle Version bei Mojang ...")
    try:
        latest_url, latest_version = await fetch_latest_linux_info()
    except Exception as e:
        log.error("Versionsabfrage fehlgeschlagen: %s", e)
        await update.message.reply_text(f"❌ Konnte aktuelle Version nicht ermitteln: {e}")
        return

    info = await query_server()
    current_version = info["version"] if info else None

    if current_version and versions_match(current_version, latest_version):
        await update.message.reply_text(f"✅ Bereits aktuell: Version `{current_version}`.")
        return

    await update.message.reply_text(
        f"⬇️ Lade Version `{latest_version}` herunter"
        + (f" (aktuell installiert: `{current_version}`)" if current_version else "")
        + " ..."
    )

    zip_path = os.path.join(UPDATE_DIR, f"bedrock-server-{latest_version}.zip")
    try:
        if not os.path.isfile(zip_path):
            await download_file(latest_url, zip_path)
        await update.message.reply_text("✅ Download fertig.")
    except Exception as e:
        log.error("Download fehlgeschlagen: %s", e)
        await update.message.reply_text(f"❌ Download fehlgeschlagen: {e}")
        return

    async def notify(text: str):
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    await perform_update_sequence(zip_path, cfg("countdown"), notify)


async def cmd_reinstall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/reinstall - installiert die aktuell laufende Version neu (repariert z.B.
    beschädigte oder veraltete Programm-/Pack-Dateien). Gleiche Sicherungen wie /update."""
    user_id = update.effective_user.id
    if user_id not in ADMIN_USER_IDS:
        await update.message.reply_text("⛔ Nur Admins dürfen den Server neu installieren.")
        return

    version = server_version
    if not version:
        await update.message.reply_text(
            "❌ Installierte Version unbekannt (steht nicht im Server-Log). "
            "Mit /update auf die neueste Version gehen."
        )
        return

    zip_path = os.path.join(UPDATE_DIR, f"bedrock-server-{version}.zip")
    if os.path.isfile(zip_path):
        await update.message.reply_text(f"📦 Nutze vorhandene Datei für Version `{version}`.",
                                        parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(f"🔍 Suche Version `{version}` bei Mojang ...",
                                        parse_mode=ParseMode.MARKDOWN)
        try:
            latest_url, latest_version = await fetch_latest_linux_info()
        except Exception as e:
            log.error("Versionsabfrage fehlgeschlagen: %s", e)
            await update.message.reply_text(f"❌ Konnte Mojang nicht abfragen: {e}")
            return
        if not versions_match(version, latest_version):
            await update.message.reply_text(
                f"❌ Für `{version}` liegt keine ZIP in `{UPDATE_DIR}`, und Mojang bietet nur "
                f"`{latest_version}` an. Mit /update auf `{latest_version}` gehen oder die "
                "ZIP dort ablegen.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        zip_path = os.path.join(UPDATE_DIR, f"bedrock-server-{latest_version}.zip")
        try:
            await download_file(latest_url, zip_path)
            await update.message.reply_text("✅ Download fertig.")
        except Exception as e:
            log.error("Download fehlgeschlagen: %s", e)
            await update.message.reply_text(f"❌ Download fehlgeschlagen: {e}")
            return

    async def notify(text: str):
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    await perform_update_sequence(zip_path, cfg("countdown"), notify, reinstall=True)


async def cmd_restore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_USER_IDS:
        await update.message.reply_text("⛔ Nur Admins dürfen ein Backup wiederherstellen.")
        return

    backups = list_backups()
    if not backups:
        await update.message.reply_text(f"Keine Backups in {BACKUP_DIR} gefunden.")
        return

    if not context.args:
        listing = "\n".join(f"- {os.path.basename(b)}" for b in backups[:10])
        await update.message.reply_text(
            "Verfügbare Backups (neueste zuerst):\n"
            f"{listing}\n\n"
            "Nutzung:\n"
            "/restore latest - neuestes Backup wiederherstellen\n"
            "/restore <dateiname> - bestimmtes Backup wiederherstellen"
        )
        return

    name_arg = context.args[0]

    if name_arg.lower() == "latest":
        backup_path = backups[0]
    else:
        backup_path = os.path.join(BACKUP_DIR, name_arg)
        if not os.path.isfile(backup_path):
            listing = "\n".join(f"- {os.path.basename(b)}" for b in backups[:10])
            await update.message.reply_text(
                f"❌ Backup nicht gefunden: {name_arg}\n\nVerfügbar:\n{listing}"
            )
            return

    async def notify(text: str):
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    await perform_restore_sequence(backup_path, cfg("countdown"), notify)


async def cmd_playtime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Nutzung: /playtime <Name>")
        return
    name = " ".join(context.args)
    total = await db_total_playtime(name)

    # Laufende Session dazuzählen, falls Spieler gerade online ist
    for active_name, join_ts in active_sessions.items():
        if active_name.lower() == name.lower():
            total += time.time() - join_ts
            break

    if total == 0:
        await update.message.reply_text(f"Keine Spielzeit für '{name}' gefunden.")
        return
    await update.message.reply_text(f"⏱ {name}: {format_duration(total)} Spielzeit insgesamt.")


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await db_top_players(5)
    if not rows:
        await update.message.reply_text("Noch keine Spielzeit-Daten vorhanden.")
        return
    lines = ["🏆 *Top Spielzeit*"]
    for i, (name, total) in enumerate(rows, start=1):
        lines.append(f"{i}. {name} - {format_duration(total)}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_spieler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not active_sessions:
        await update.message.reply_text("Aktuell ist niemand auf dem Server online.")
        return
    now = time.time()
    lines = ["👥 *Aktuell online:*"]
    for name, join_ts in active_sessions.items():
        lines.append(f"- {name} (seit {format_duration(now - join_ts)})")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Verfügbare Befehle:\n"
        "/status - Serverstatus, Spieleranzahl, Version\n"
        "/spieler - Wer ist aktuell online\n"
        "/error [warn] [anzahl] - Fehler (oder Warnungen) aus dem Server-Log seit dem letzten Start\n"
        "/restart [sekunden] - Neustart mit Countdown (nur berechtigte Nutzer)\n"
        "/backup - Manuelles Backup erstellen (nur berechtigte Nutzer)\n"
        "/backups - Backups ansehen und löschen (nur Admins)\n"
        "/autorestart - Geplanten Neustart verwalten (nur Admins)\n"
        "/settings - Einstellungen ansehen; ändern nur Admins\n"
        "/update - Prüft automatisch auf die neueste Version, lädt sie herunter "
        "und installiert sie: Stop, Backup, Entpacken "
        "(worlds/Configs bleiben erhalten), Start (nur Admins)\n"
        "/reinstall - Installierte Version neu einspielen (Backup vorher, Welten bleiben; nur Admins)\n"
        "/restore [datei|latest] - Backup wiederherstellen, überschreibt "
        "worlds/Configs (nur Admins)\n"
        "/playtime <Name> - Spielzeit eines Spielers\n"
        "/top - Top-5-Rangliste nach Spielzeit\n"
    )
    if cfg("neustart_aktiv"):
        text += (
            f"\nGeplanter Neustart täglich um "
            f"{cfg('neustart_zeit')} Uhr."
        )
    await update.message.reply_text(text)


# --- Hintergrund-Task 1: Server-Offline/Online-Erkennung -------------------
async def poll_server_availability(app: Application):
    global server_was_online
    while True:
        try:
            if maintenance_mode:
                # Während eines geplanten Stop/Update/Restore keine
                # Fehlalarme auslösen - Status wird nicht mitgeschrieben,
                # damit nach der Wartung sauber weiter verglichen wird.
                await asyncio.sleep(cfg("pruef_intervall"))
                continue

            info = await query_server()
            is_online = info is not None
            if server_was_online is not None and is_online != server_was_online:
                if is_online:
                    await app.bot.send_message(GROUP_CHAT_ID, "🟢 Server ist wieder online.")
                else:
                    await app.bot.send_message(
                        GROUP_CHAT_ID,
                        "🔴 Server ist offline gegangen (evtl. Absturz - Logs prüfen).",
                    )
            server_was_online = is_online
        except Exception as e:
            log.error("Fehler im Verfügbarkeits-Polling: %s", e)
        await asyncio.sleep(cfg("pruef_intervall"))


# --- Hintergrund-Task 2: Live-Log-Watcher -----------------------------------
async def _iter_log_lines():
    if LOG_MODE == "file":
        if not LOG_FILE_PATH:
            raise RuntimeError("LOG_MODE=file gesetzt, aber LOG_FILE_PATH fehlt.")
        cmd = ["tail", "-F", "-n", "0", LOG_FILE_PATH]
    else:
        cmd = ["journalctl", "-u", SERVICE_NAME, "-f", "-n", "0", "--output=cat"]

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            yield line.decode(errors="ignore").rstrip()
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def watch_logs(app: Application):
    global last_error_line, server_version, server_booting
    while True:
        try:
            log.info("Log-Watcher gestartet (LOG_MODE=%s)", LOG_MODE)
            async for line in _iter_log_lines():
                version_match = VERSION_RE.search(line)
                if version_match:
                    # Erste Zeile eines Serverstarts: Sammlung zurücksetzen
                    server_version = version_match.group(1)
                    server_booting = True
                    reset_log_issues()
                if SERVER_STARTED_RE.search(line):
                    if active_sessions:
                        # Frischer Serverstart: Sessions aus der Zeit davor sind hinfällig
                        log.info("Server gestartet - verwerfe veraltete Sessions: %s",
                                 ", ".join(active_sessions))
                        active_sessions.clear()
                    if server_booting:
                        server_booting = False
                        await app.bot.send_message(
                            GROUP_CHAT_ID, startup_summary_text(), parse_mode=ParseMode.MARKDOWN,
                        )
                    continue

                join_leave = JOIN_LEAVE_RE.search(line)
                if join_leave:
                    action, name = join_leave.group(1), join_leave.group(2).strip()
                    now = time.time()
                    if action == "connected":
                        active_sessions[name] = now
                        text = f"🟢 *{name}* ist dem Server beigetreten."
                    else:
                        join_ts = active_sessions.pop(name, None)
                        if join_ts is not None:
                            await db_record_session(name, join_ts, now)
                            duration = format_duration(now - join_ts)
                            text = f"🔴 *{name}* hat den Server verlassen (Session: {duration})."
                        else:
                            text = f"🔴 *{name}* hat den Server verlassen."
                    await app.bot.send_message(GROUP_CHAT_ID, text, parse_mode=ParseMode.MARKDOWN)
                    continue

                kind = classify_log_line(line)
                if kind is None:
                    continue
                was_booting = server_booting
                record_log_issue(kind, line)
                # Einzelmeldung nur für Fehler nach dem Start (nie für Warnungen
                # und nie für Meldungen während des Starts)
                if kind == "error" and not was_booting and cfg("fehler_alarm"):
                    # Zeitstempel für den Vergleich entfernen, sonst ist jede
                    # Zeile "neu" und die Unterdrückung von Wiederholungen greift nie
                    normalized = ERROR_TIMESTAMP_RE.sub("[", line, count=1)
                    if normalized != last_error_line:
                        last_error_line = normalized
                        await app.bot.send_message(
                            GROUP_CHAT_ID, f"⚠️ *Fehler im Server-Log:*\n`{line[:300]}`",
                            parse_mode=ParseMode.MARKDOWN,
                        )
        except Exception as e:
            log.error("Log-Watcher abgebrochen, starte in 5s neu: %s", e)
        await asyncio.sleep(5)


# --- Hintergrund-Task 3: Automatisches Backup -------------------------------
async def scheduled_backup(app: Application):
    while True:
        if not cfg("backup_aktiv"):
            log.info("Automatisches Backup ist deaktiviert (/settings backup_aktiv an).")
            await wait_or_change("backup", 86400)
            continue

        target = next_occurrence(cfg("backup_zeit"))
        log.info("Nächstes automatisches Backup: %s", target)
        if await wait_or_change("backup", seconds_until(target)):
            continue  # Einstellung geändert: Zeitpunkt neu berechnen

        if maintenance_mode:
            log.info("Automatisches Backup übersprungen - Wartung läuft.")
            await app.bot.send_message(
                GROUP_CHAT_ID, "⏭ Automatisches Backup übersprungen, weil gerade ein Neustart/Update läuft."
            )
            continue
        try:
            path, size = await run_backup()
            size_mb = size / (1024 * 1024)
            await app.bot.send_message(
                GROUP_CHAT_ID,
                f"💾 Automatisches Backup erstellt: `{os.path.basename(path)}` ({size_mb:.1f} MB)\n"
                f"{last_retention_summary}",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            log.error("Automatisches Backup fehlgeschlagen: %s", e)
            await app.bot.send_message(GROUP_CHAT_ID, f"❌ Automatisches Backup fehlgeschlagen: {e}")


# --- Hintergrund-Task 4: Geplante Neustarts ---------------------------------
async def scheduled_restart_task(app: Application):
    while True:
        if not cfg("neustart_aktiv"):
            await wait_or_change("restart", 86400)
            continue

        target = next_occurrence(cfg("neustart_zeit"))
        log.info("Nächster geplanter Neustart: %s", target)
        if await wait_or_change("restart", seconds_until(target)):
            continue  # Einstellung geändert: Zeitpunkt neu berechnen

        global skip_next_restart
        if skip_next_restart:
            skip_next_restart = False
            log.info("Geplanter Neustart auf Wunsch eines Admins übersprungen.")
            await app.bot.send_message(
                GROUP_CHAT_ID, "⏭ Der geplante Neustart wird heute auf Wunsch eines Admins übersprungen."
            )
            continue

        info = await query_server()
        if cfg("neustart_ueberspringen") and (info is None or info["players_online"] == 0):
            log.info("Geplanter Neustart übersprungen - niemand online.")
            continue

        async def notify(text: str):
            await app.bot.send_message(GROUP_CHAT_ID, text)

        await notify("🕓 Geplanter täglicher Server-Neustart steht an.")
        await perform_restart_sequence(cfg("neustart_countdown"), notify)


def bootstrap_active_sessions():
    """Wird einmalig beim Bot-Start ausgeführt: liest die letzten Journal-
    Einträge, um herauszufinden, wer JETZT bereits online ist (z.B. weil
    der Bot neugestartet wurde, während Spieler schon verbunden waren).
    Nur der In-Memory-Zustand wird gesetzt, nicht die Statistik-DB."""
    if LOG_MODE != "journalctl":
        return  # Reconciliation nur für journalctl-Modus implementiert

    try:
        result = subprocess.run(
            ["journalctl", "-u", SERVICE_NAME, "-n", "2000", "-o", "short-iso", "--no-pager"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as e:
        log.error("Konnte Journal-Historie für Reconciliation nicht lesen: %s", e)
        return

    global server_version, server_booting
    for line in result.stdout.splitlines():
        version_match = VERSION_RE.search(line)
        if version_match:
            server_version = version_match.group(1)
            server_booting = True
            reset_log_issues()
        if SERVER_STARTED_RE.search(line):
            active_sessions.clear()  # Sessions vor einem Neustart sind hinfällig
            server_booting = False
            continue

        match = JOIN_LEAVE_RE.search(line)
        if not match:
            kind = classify_log_line(line)
            if kind:
                record_log_issue(kind, strip_log_prefix(line))
            continue
        action, name = match.group(1), match.group(2).strip()

        ts = time.time()
        ts_match = re.match(r"^(\S+)", line)
        if ts_match:
            try:
                ts = datetime.fromisoformat(ts_match.group(1)).timestamp()
            except ValueError:
                pass

        if action == "connected":
            active_sessions[name] = ts
        else:
            active_sessions.pop(name, None)

    if server_version is None:
        # Startzeile liegt außerhalb der letzten 2000 Zeilen: gezielt suchen
        try:
            found = subprocess.run(
                ["journalctl", "-u", SERVICE_NAME, "-o", "cat", "--no-pager",
                 "-g", r"INFO\] Version:"],
                capture_output=True, text=True, timeout=30,
            )
            for line in found.stdout.splitlines():
                version_match = VERSION_RE.search(line)
                if version_match:
                    server_version = version_match.group(1)
        except Exception as e:
            log.warning("Serverversion nicht ermittelbar: %s", e)
    log.info("Erkannte Serverversion: %s", server_version or "unbekannt")

    if active_sessions:
        log.info("Reconciliation: aktuell online laut Log: %s", ", ".join(active_sessions))
    else:
        log.info("Reconciliation: laut Log ist aktuell niemand online.")


async def post_init(app: Application):
    app.create_task(poll_server_availability(app))
    app.create_task(watch_logs(app))
    app.create_task(scheduled_backup(app))
    app.create_task(scheduled_restart_task(app))


def main():
    init_db()
    load_settings()
    bootstrap_active_sessions()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("spieler", cmd_spieler))
    app.add_handler(CommandHandler("error", cmd_error))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("backups", cmd_backups))
    app.add_handler(CommandHandler("autorestart", cmd_autorestart))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CommandHandler("update", cmd_update))
    app.add_handler(CommandHandler("reinstall", cmd_reinstall))
    app.add_handler(CommandHandler("restore", cmd_restore))
    app.add_handler(CommandHandler("playtime", cmd_playtime))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))

    log.info("Bot gestartet. Überwache %s:%s (Dienst: %s)", SERVER_HOST, SERVER_PORT, SERVICE_NAME)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
