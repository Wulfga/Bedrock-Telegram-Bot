#!/usr/bin/env python3
"""
Telegram-Bot zur Steuerung/Überwachung eines Minecraft Bedrock Servers.

Befehle:
  /status    - Serverstatus (online/offline, Spieleranzahl, Version)
  /spieler   - Wer ist aktuell online
  /restart   - Neustart mit Countdown-Ankündigung im Spiel (nur ALLOWED_USER_IDS)
  /backup    - Manuelles Backup der Weltdaten (nur ALLOWED_USER_IDS)
  /update    - Prüft automatisch auf neue Version, lädt sie herunter und
               installiert sie: Stop, Backup, Update, Start - worlds/Configs
               bleiben erhalten (nur ADMIN_USER_IDS)
  /restore   - Stellt ein Backup wieder her, überschreibt worlds/Configs
               (nur ADMIN_USER_IDS)
  /playtime  - Spielzeit eines Spielers anzeigen: /playtime <Name>
  /top       - Top-5-Rangliste nach Spielzeit
  /help      - Hilfe

Hintergrund-Tasks:
  - Offline/Online-Erkennung des Servers
  - Live-Log-Watcher: Join/Leave (mit Klarnamen + Spielzeit-Tracking) und
    Fehler-/Absturz-Erkennung (ERROR/Exception/FATAL im Log)
  - Automatisches, zeitgesteuertes Backup (BACKUP_INTERVAL_HOURS)
  - Geplanter täglicher Neustart (SCHEDULED_RESTART_ENABLED)

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
from datetime import datetime, timedelta

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
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))

COMMAND_FIFO_PATH = os.environ.get("COMMAND_FIFO_PATH", "/opt/bedrock/command_input")
RESTART_COUNTDOWN_SECONDS = int(os.environ.get("RESTART_COUNTDOWN_SECONDS", "60"))
RESTART_ANNOUNCE_THRESHOLDS = [30, 10, 5, 4, 3, 2, 1]

LOG_MODE = os.environ.get("LOG_MODE", "journalctl")
LOG_FILE_PATH = os.environ.get("LOG_FILE_PATH", "")

# Backups
BACKUP_DIR = os.environ.get("BACKUP_DIR", "/opt/bedrock-backups")
BACKUP_INTERVAL_HOURS = float(os.environ.get("BACKUP_INTERVAL_HOURS", "24"))
BACKUP_RETENTION_COUNT = int(os.environ.get("BACKUP_RETENTION_COUNT", "7"))

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
UPDATE_PROTECTED_PATHS = [
    p.strip()
    for p in os.environ.get(
        "UPDATE_PROTECTED_PATHS",
        "worlds,permissions.json,allowlist.json,server.properties,"
        "resource_packs,behavior_packs,valid_known_packs.json",
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

# Geplante Neustarts
SCHEDULED_RESTART_ENABLED = os.environ.get("SCHEDULED_RESTART_ENABLED", "false").lower() == "true"
SCHEDULED_RESTART_HOUR = int(os.environ.get("SCHEDULED_RESTART_HOUR", "4"))
SCHEDULED_RESTART_MINUTE = int(os.environ.get("SCHEDULED_RESTART_MINUTE", "0"))
SCHEDULED_RESTART_COUNTDOWN_SECONDS = int(
    os.environ.get("SCHEDULED_RESTART_COUNTDOWN_SECONDS", str(RESTART_COUNTDOWN_SECONDS))
)
SCHEDULED_RESTART_SKIP_IF_EMPTY = (
    os.environ.get("SCHEDULED_RESTART_SKIP_IF_EMPTY", "true").lower() == "true"
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

JOIN_LEAVE_RE = re.compile(r"Player (connected|disconnected):\s*([^,]+),")
VERSION_RE = re.compile(r"INFO\] Version:\s*(\d+(?:\.\d+)+)")
SERVER_STARTED_RE = re.compile(r"INFO\] Server started\.")
ERROR_RE = re.compile(r"\b(ERROR|Exception|FATAL)\b", re.IGNORECASE)
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


# --- Backup-Logik ------------------------------------------------------------
def _do_backup() -> tuple[str, int]:
    """Erstellt ein tar.gz-Backup von SERVER_DIR. Gibt (pfad, größe_bytes) zurück."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    archive_path_base = os.path.join(BACKUP_DIR, f"backup_{timestamp}")
    archive_path = shutil.make_archive(archive_path_base, "gztar", SERVER_DIR)
    size = os.path.getsize(archive_path)

    # Alte Backups aufräumen, nur die letzten BACKUP_RETENTION_COUNT behalten
    backups = sorted(glob.glob(os.path.join(BACKUP_DIR, "backup_*.tar.gz")))
    while len(backups) > BACKUP_RETENTION_COUNT:
        oldest = backups.pop(0)
        try:
            os.remove(oldest)
            log.info("Altes Backup gelöscht: %s", oldest)
        except OSError as e:
            log.error("Konnte altes Backup nicht löschen: %s", e)

    return archive_path, size


async def run_backup() -> tuple[str, int]:
    return await asyncio.get_event_loop().run_in_executor(None, _do_backup)


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
    with urllib.request.urlopen(req, timeout=180) as resp, open(dest_path, "wb") as f:
        shutil.copyfileobj(resp, f)


async def download_file(url: str, dest_path: str):
    await asyncio.get_event_loop().run_in_executor(None, _download_file, url, dest_path)


# --- Update-Logik --------------------------------------------------------
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


async def perform_update_sequence(zip_path: str, seconds: int, notify):
    zip_name = os.path.basename(zip_path)

    await perform_countdown(seconds, notify, f"Server-Update ({zip_name})")

    await notify("🛑 Stoppe Server ...")
    if not await do_stop_service(notify):
        return

    await notify("💾 Erstelle Backup vor dem Update ...")
    try:
        backup_path, backup_size = await run_backup()
        await notify(
            f"✅ Backup fertig: `{os.path.basename(backup_path)}` "
            f"({backup_size / (1024*1024):.1f} MB)",
        )
    except Exception as e:
        log.error("Backup vor Update fehlgeschlagen: %s", e)
        global maintenance_mode
        maintenance_mode = False
        await notify(
            f"❌ Backup fehlgeschlagen ({e}) - Update wird abgebrochen, "
            "Server bleibt aus Sicherheitsgründen gestoppt. Bitte manuell prüfen."
        )
        return

    await notify(
        "📦 Entpacke Update - `worlds/`, `permissions.json`, `allowlist.json`, "
        "`server.properties`, Resource-/Behavior-Packs bleiben unangetastet ..."
    )
    try:
        await run_extract_update(zip_path)
    except Exception as e:
        log.error("Update-Entpacken fehlgeschlagen: %s", e)
        maintenance_mode = False
        await notify(
            f"❌ Update fehlgeschlagen beim Entpacken ({e}). "
            "Server bleibt gestoppt - Backup ist vorhanden, bitte manuell prüfen."
        )
        return

    await notify("🚀 Starte Server mit neuer Version ...")
    if not await do_start_service(notify):
        return

    await notify("✅ Update abgeschlossen und Server läuft wieder.")


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
        safety_path, safety_size = await run_backup()
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


async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Du bist nicht berechtigt, den Server neuzustarten.")
        log.warning("Unautorisierter Restart-Versuch von user_id=%s", user_id)
        return

    seconds = RESTART_COUNTDOWN_SECONDS
    if context.args:
        try:
            seconds = max(0, int(context.args[0]))
        except ValueError:
            await update.message.reply_text(
                f"Ungültige Angabe, nutze Standard-Countdown ({RESTART_COUNTDOWN_SECONDS}s)."
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

    await perform_update_sequence(zip_path, RESTART_COUNTDOWN_SECONDS, notify)


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

    await perform_restore_sequence(backup_path, RESTART_COUNTDOWN_SECONDS, notify)


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
        "/restart [sekunden] - Neustart mit Countdown (nur berechtigte Nutzer)\n"
        "/backup - Manuelles Backup erstellen (nur berechtigte Nutzer)\n"
        "/update - Prüft automatisch auf die neueste Version, lädt sie herunter "
        "und installiert sie: Stop, Backup, Entpacken "
        "(worlds/Configs bleiben erhalten), Start (nur Admins)\n"
        "/restore [datei|latest] - Backup wiederherstellen, überschreibt "
        "worlds/Configs (nur Admins)\n"
        "/playtime <Name> - Spielzeit eines Spielers\n"
        "/top - Top-5-Rangliste nach Spielzeit\n"
    )
    if SCHEDULED_RESTART_ENABLED:
        text += (
            f"\nGeplanter Neustart täglich um "
            f"{SCHEDULED_RESTART_HOUR:02d}:{SCHEDULED_RESTART_MINUTE:02d} Uhr."
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
                await asyncio.sleep(POLL_INTERVAL)
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
        await asyncio.sleep(POLL_INTERVAL)


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
    global last_error_line, server_version
    while True:
        try:
            log.info("Log-Watcher gestartet (LOG_MODE=%s)", LOG_MODE)
            async for line in _iter_log_lines():
                version_match = VERSION_RE.search(line)
                if version_match:
                    server_version = version_match.group(1)
                if SERVER_STARTED_RE.search(line) and active_sessions:
                    # Frischer Serverstart: Sessions aus der Zeit davor sind hinfällig
                    log.info("Server gestartet - verwerfe veraltete Sessions: %s",
                             ", ".join(active_sessions))
                    active_sessions.clear()
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

                if ERROR_RE.search(line):
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
    interval_seconds = BACKUP_INTERVAL_HOURS * 3600
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            path, size = await run_backup()
            size_mb = size / (1024 * 1024)
            await app.bot.send_message(
                GROUP_CHAT_ID,
                f"💾 Automatisches Backup erstellt: {os.path.basename(path)} ({size_mb:.1f} MB)",
            )
        except Exception as e:
            log.error("Automatisches Backup fehlgeschlagen: %s", e)
            await app.bot.send_message(GROUP_CHAT_ID, f"❌ Automatisches Backup fehlgeschlagen: {e}")


# --- Hintergrund-Task 4: Geplante Neustarts ---------------------------------
async def scheduled_restart_task(app: Application):
    if not SCHEDULED_RESTART_ENABLED:
        log.info("Geplante Neustarts sind deaktiviert (SCHEDULED_RESTART_ENABLED=false).")
        return

    while True:
        now = datetime.now()
        target = now.replace(
            hour=SCHEDULED_RESTART_HOUR, minute=SCHEDULED_RESTART_MINUTE,
            second=0, microsecond=0,
        )
        if target <= now:
            target += timedelta(days=1)
        wait_seconds = (target - now).total_seconds()
        log.info("Nächster geplanter Neustart um %s (in %.0fs)", target, wait_seconds)
        await asyncio.sleep(wait_seconds)

        info = await query_server()
        if SCHEDULED_RESTART_SKIP_IF_EMPTY and (info is None or info["players_online"] == 0):
            log.info("Geplanter Neustart übersprungen - niemand online.")
            continue

        async def notify(text: str):
            await app.bot.send_message(GROUP_CHAT_ID, text)

        await notify("🕓 Geplanter täglicher Server-Neustart steht an.")
        await perform_restart_sequence(SCHEDULED_RESTART_COUNTDOWN_SECONDS, notify)


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

    global server_version
    for line in result.stdout.splitlines():
        version_match = VERSION_RE.search(line)
        if version_match:
            server_version = version_match.group(1)
        if SERVER_STARTED_RE.search(line):
            active_sessions.clear()  # Sessions vor einem Neustart sind hinfällig
            continue

        match = JOIN_LEAVE_RE.search(line)
        if not match:
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
    bootstrap_active_sessions()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("spieler", cmd_spieler))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CommandHandler("update", cmd_update))
    app.add_handler(CommandHandler("restore", cmd_restore))
    app.add_handler(CommandHandler("playtime", cmd_playtime))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))

    log.info("Bot gestartet. Überwache %s:%s (Dienst: %s)", SERVER_HOST, SERVER_PORT, SERVICE_NAME)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
