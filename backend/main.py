"""
AmneziaWG Web Panel — FastAPI backend.
- Управление peers (awg syncconf)
- Учет трафика с разбивкой по месяцам
- Логирование подключений (handshake: IP клиента, время)
"""

import os
import sys
import json
import secrets
import hashlib
import subprocess
import sqlite3
import asyncio
import threading
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Dict
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Depends, status, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import jwt
import bcrypt
import qrcode
import io
import base64
import requests

from multi_server import (
    from_server_row,
    check_server_health,
    get_server_peers,
    get_server_pubkey,
    create_remote_peer,
    delete_remote_peer,
    service_action,
)
import crypto
import aggregate
import discover
import notify
import realtime
from update_utils import is_newer_version
from fastapi import WebSocket, WebSocketDisconnect

# ─── Config ────────────────────────────────────────────────────────────
DATA_DIR = Path(os.getenv("AWG_DATA_DIR", "/data"))
WG_CONFIG = Path(os.getenv("AWG_CONFIG_PATH", "/data/awg0.conf"))
DB_PATH = Path(os.getenv("AWG_DB_PATH", "/data/panel.db"))
SECRET_KEY = os.getenv("PANEL_SECRET_KEY", secrets.token_urlsafe(32))
JWT_ALG = "HS256"
JWT_HOURS = 24
TRAFFIC_COLLECT_INTERVAL = int(os.getenv("TRAFFIC_INTERVAL", "300"))  # 5 минут
APP_VERSION = os.getenv("PANEL_VERSION", "1.1.5").lstrip("v")
PANEL_REPO = os.getenv("PANEL_REPO", "sllikmll/amnezia-panel")
PANEL_IMAGE = os.getenv("PANEL_IMAGE", "ghcr.io/sllikmll/amnezia-panel:latest")
PANEL_UPDATE_COMMAND = os.getenv("PANEL_UPDATE_COMMAND", "/usr/local/bin/update-panel")
PANEL_UPDATE_TIMEOUT = int(os.getenv("PANEL_UPDATE_TIMEOUT", "1800"))
UPDATE_STATE = {"running": False, "status": "idle", "started_at": None, "finished_at": None, "target_version": None, "log": ""}

# ─── DB ────────────────────────────────────────────────────────────────
def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS peers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            server_id INTEGER NOT NULL DEFAULT 0,
            name TEXT NOT NULL,
            public_key TEXT NOT NULL,
            private_key TEXT NOT NULL,
            preshared_key TEXT NOT NULL,
            ip_address TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            enabled INTEGER DEFAULT 1,
            download_bytes INTEGER DEFAULT 0,
            upload_bytes INTEGER DEFAULT 0,
            last_seen TIMESTAMP,
            last_endpoint TEXT,
            UNIQUE(server_id, name),
            UNIQUE(server_id, public_key)
        );
        CREATE TABLE IF NOT EXISTS admins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS servers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            host TEXT NOT NULL,
            port INTEGER NOT NULL DEFAULT 22,
            username TEXT NOT NULL,
            auth_type TEXT NOT NULL DEFAULT 'password',
            password TEXT,
            key_path TEXT,
            key_data TEXT,
            amnezia_container TEXT NOT NULL DEFAULT 'awg-tunnel',
            amnezia_iface TEXT NOT NULL DEFAULT 'awg0',
            listen_port INTEGER NOT NULL DEFAULT 51820,
            endpoint TEXT,
            notes TEXT,
            status TEXT NOT NULL DEFAULT 'unknown',
            last_check TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS traffic_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            peer_id INTEGER NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            download_bytes INTEGER NOT NULL,
            upload_bytes INTEGER NOT NULL,
            FOREIGN KEY (peer_id) REFERENCES peers(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS connection_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            peer_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            endpoint TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (peer_id) REFERENCES peers(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user TEXT NOT NULL,
            action TEXT NOT NULL,
            target TEXT,
            details TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS notification_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            config TEXT NOT NULL,
            prefs TEXT,
            enabled INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS notification_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event TEXT NOT NULL,
            peer_id INTEGER,
            server_id INTEGER,
            sent INTEGER DEFAULT 1,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS user_prefs (
            user TEXT PRIMARY KEY,
            notify_handshake INTEGER DEFAULT 1,
            notify_server_down INTEGER DEFAULT 1,
            notify_traffic_threshold INTEGER DEFAULT 0,
            traffic_threshold_gb INTEGER DEFAULT 100,
            last_notify_traffic_check TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS server_prefs (
            server_id INTEGER PRIMARY KEY,
            notify_handshake INTEGER DEFAULT 1,
            notify_traffic INTEGER DEFAULT 0,
            traffic_threshold_gb INTEGER DEFAULT 100
        );
        CREATE INDEX IF NOT EXISTS idx_snapshots_peer_time
            ON traffic_snapshots(peer_id, timestamp);
        CREATE INDEX IF NOT EXISTS idx_log_peer_time
            ON connection_log(peer_id, timestamp);
    """)
    conn.commit()

    # Миграция: добавляем колонки/таблицы если их нет
    for table, column, default in [
        ("peers", "last_endpoint", "TEXT"),
        ("peers", "server_id", "INTEGER NOT NULL DEFAULT 0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {default}")
            conn.commit()
        except sqlite3.OperationalError:
            pass

    conn.close()

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# ─── Models ────────────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str
    password: str

class PeerCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)

class PeerResponse(BaseModel):
    id: int
    name: str
    public_key: str
    ip_address: str
    enabled: bool
    created_at: str
    last_seen: Optional[str]
    last_endpoint: Optional[str]
    download_bytes: int
    upload_bytes: int

class PeerWithConfig(PeerResponse):
    config: str
    qr_code: str  # base64 PNG

class TrafficMonth(BaseModel):
    year: int
    month: int
    download_bytes: int
    upload_bytes: int
    total_bytes: int

class ConnectionLogEntry(BaseModel):
    id: int
    peer_id: int
    peer_name: str
    event_type: str
    endpoint: Optional[str]
    timestamp: str

class ServerCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    host: str = Field(..., min_length=1)
    port: int = 22
    username: str = Field(..., min_length=1)
    auth_type: str = "password"
    password: Optional[str] = None
    key_path: Optional[str] = None
    key_data: Optional[str] = None
    amnezia_container: str = "awg-tunnel"
    amnezia_iface: str = "awg0"
    listen_port: int = 51820
    endpoint: Optional[str] = None
    notes: Optional[str] = None

class ServerUpdate(BaseModel):
    name: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    username: Optional[str] = None
    auth_type: Optional[str] = None
    password: Optional[str] = None
    key_path: Optional[str] = None
    key_data: Optional[str] = None
    amnezia_container: Optional[str] = None
    amnezia_iface: Optional[str] = None
    listen_port: Optional[int] = None
    endpoint: Optional[str] = None
    notes: Optional[str] = None

class ServerResponse(BaseModel):
    id: int
    name: str
    host: str
    port: int
    username: str
    auth_type: str
    amnezia_container: str
    amnezia_iface: str
    listen_port: int
    endpoint: Optional[str]
    notes: Optional[str]
    status: str
    last_check: Optional[str]
    created_at: str
    has_password: bool = False
    has_key: bool = False

class ServerHealthResponse(BaseModel):
    ok: bool
    host: str
    container_status: str
    awg_status: str
    listen_port: Optional[str] = None
    error: Optional[str] = None
    peer_count: int = 0
    total_download: int = 0
    total_upload: int = 0

# ─── Auth ──────────────────────────────────────────────────────────────
security = HTTPBearer()

def create_token(username: str) -> str:
    payload = {
        "sub": username,
        "exp": datetime.utcnow() + timedelta(hours=JWT_HOURS)
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=JWT_ALG)

def verify_token(creds: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(creds.credentials, SECRET_KEY, algorithms=[JWT_ALG])
        return payload["sub"]
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def check_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())

def ensure_admin():
    conn = get_db()
    row = conn.execute("SELECT COUNT(*) FROM admins").fetchone()
    conn.close()
    if row[0] == 0:
        default_pwd = os.getenv("PANEL_ADMIN_PASSWORD", "admin")
        u, p = os.getenv("PANEL_ADMIN_USER", "admin"), default_pwd
        conn = get_db()
        conn.execute(
            "INSERT INTO admins (username, password_hash) VALUES (?, ?)",
            (u, hash_password(p))
        )
        conn.commit()
        conn.close()
        print(f"[init] Created admin: {u}")

# ─── AmneziaWG ops ─────────────────────────────────────────────────────
def load_wg_config() -> dict:
    if not WG_CONFIG.exists():
        return {"interface": {}, "peers": []}
    interface = {}
    peers = []
    current_peer = None
    with open(WG_CONFIG) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line == "[Interface]":
                interface = {}
                current_peer = None
            elif line == "[Peer]":
                if current_peer:
                    peers.append(current_peer)
                current_peer = {}
            elif "=" in line:
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip()
                if current_peer is not None:
                    current_peer[key] = val
                else:
                    interface[key] = val
    if current_peer:
        peers.append(current_peer)
    return {"interface": interface, "peers": peers}

def get_server_public_key(server_row: dict = None) -> str:
    if server_row is None or server_row.get("id") == 0:
        pub_path = Path("/data/server.pub")
        if pub_path.exists():
            return pub_path.read_text().strip()
        return ""
    return get_server_pubkey(server_row)

def generate_keys() -> tuple:
    priv = subprocess.run(["wg", "genkey"], capture_output=True, text=True, check=True).stdout.strip()
    pub = subprocess.run(["wg", "pubkey"], input=priv, capture_output=True, text=True, check=True).stdout.strip()
    psk = subprocess.run(["wg", "genpsk"], capture_output=True, text=True, check=True).stdout.strip()
    return priv, pub, psk

def next_ip(peers: list) -> str:
    used = {p["ip_address"] for p in peers}
    base = "10.8.1."
    for i in range(2, 255):
        ip = f"{base}{i}"
        if ip not in used:
            return ip
    raise HTTPException(500, "No available IPs")

def wg_sync_conf(peer: dict, remove: bool = False):
    """Применяет изменения через awg syncconf (добавляет peer без сброса остальных)."""
    if remove:
        cmd_input = f"public_key = {peer['public_key']}\nremove\n"
        try:
            subprocess.run(
                ["docker", "exec", "-i", "awg-tunnel", "/usr/local/bin/awg", "syncconf", "awg0"],
                input=cmd_input, text=True, check=True, capture_output=True, timeout=10
            )
        except subprocess.CalledProcessError as e:
            # Fallback: используем awg set с subcommand remove
            try:
                subprocess.run(
                    ["docker", "exec", "awg-tunnel", "/usr/local/bin/awg", "set", "awg0",
                     "peer", peer["public_key"], "remove"],
                    check=True, capture_output=True, timeout=10
                )
            except Exception:
                pass
    else:
        # Используем awg syncconf через stdin - добавляет peer, не сбрасывая других
        cmd_input = (
            f"public_key = {peer['public_key']}\n"
            f"preshared_key = {peer['preshared_key']}\n"
            f"allowed_ips = {peer['ip_address']}/32\n"
        )
        try:
            subprocess.run(
                ["docker", "exec", "-i", "awg-tunnel", "/usr/local/bin/awg", "syncconf", "awg0"],
                input=cmd_input, text=True, check=True, capture_output=True, timeout=10
            )
        except subprocess.CalledProcessError as e:
            stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else e.stderr
            # Если syncconf не поддерживается — fallback на set с preshared-key через файл
            print(f"awg syncconf error, trying alt method: {stderr}", file=sys.stderr)
            try:
                # Через awg set peer ... preshared-key (без дефиса в ключе нельзя)
                # Создаём файл PSK в awg-tunnel
                psk_content = peer["preshared_key"]
                with open("/tmp/psk.tmp", "w") as f:
                    f.write(psk_content)
                subprocess.run(
                    ["docker", "cp", "/tmp/psk.tmp", "awg-tunnel:/tmp/psk.tmp"],
                    check=True, capture_output=True, timeout=10
                )
                os.unlink("/tmp/psk.tmp")
                subprocess.run(
                    ["docker", "exec", "awg-tunnel", "/usr/local/bin/awg", "set", "awg0",
                     "peer", peer["public_key"],
                     "preshared-key", "/tmp/psk.tmp",
                     "allowed-ips", f"{peer['ip_address']}/32"],
                    check=True, capture_output=True, timeout=10
                )
            except Exception as e2:
                print(f"awg set fallback error: {e2}", file=sys.stderr)

def gen_client_config_remote(private_key: str, ip: str, psk: str, server_pubkey: str, endpoint: str) -> str:
    return (
        "[Interface]\n"
        f"PrivateKey = {private_key}\n"
        f"Address = {ip}/32\n"
        f"DNS = 1.1.5.1, 8.8.8.8\n"
        "\n[Peer]\n"
        f"PublicKey = {server_pubkey}\n"
        f"PresharedKey = {psk}\n"
        f"Endpoint = {endpoint}\n"
        "AllowedIPs = 0.0.0.0/0, ::/0\n"
        "PersistentKeepalive = 25\n"
    )

def gen_client_config(peer: dict, server_pubkey: str) -> str:
    endpoint = os.getenv("AWG_ENDPOINT", "vpn.example.com:51820")
    return (
        "[Interface]\n"
        f"PrivateKey = {peer['private_key']}\n"
        f"Address = {peer['ip_address']}/32\n"
        f"DNS = 1.1.5.1, 8.8.8.8\n"
        "\n[Peer]\n"
        f"PublicKey = {server_pubkey}\n"
        f"PresharedKey = {peer['preshared_key']}\n"
        f"Endpoint = {endpoint}\n"
        "AllowedIPs = 0.0.0.0/0, ::/0\n"
        "PersistentKeepalive = 25\n"
    )

# ─── Парсинг awg show all dump ─────────────────────────────────────────
def parse_awg_dump() -> List[Dict]:
    """
    Парсит вывод `awg show all dump` через awg-tunnel контейнер.
    """
    # Пробуем напрямую (если awg доступен в текущем контейнере)
    try:
        result = subprocess.run(
            ["awg", "show", "all", "dump"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout:
            return _parse_dump_lines(result.stdout)
    except Exception:
        pass

    # Fallback: docker exec в awg-tunnel
    try:
        result = subprocess.run(
            ["docker", "exec", "awg-tunnel", "/usr/local/bin/awg", "show", "all", "dump"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout:
            return _parse_dump_lines(result.stdout)
    except Exception as e:
        print(f"awg show via docker exec failed: {e}", file=sys.stderr)

    return []


def _parse_dump_lines(stdout: str) -> List[Dict]:
    """Парсит текст вывода awg show all dump.

    Формат amneziawg-tools (9 полей на строку):
    awg0\t<pub>\t<psk>\t<endpoint>\t<allowed_ips>\t<transfer_rx>\t<transfer_tx>\t<persistent_keepalive>\t<last_handshake>
    """
    peers_data = []
    lines = stdout.strip().split("\n")
    if not lines:
        return peers_data

    for line in lines:
        if not line:
            continue
        parts = line.split("\t")
        # amneziawg-tools: 9 полей
        if len(parts) < 9:
            continue
        try:
            # Первое поле = имя интерфейса (awg0), парсим со второго
            public_key = parts[1]
            if len(public_key) < 40 or "(none)" in public_key:
                continue
            endpoint = parts[3] if parts[3] != "(none)" else ""
            transfer_rx = int(parts[5]) if parts[5].isdigit() else 0
            transfer_tx = int(parts[6]) if parts[6].isdigit() else 0
            last_handshake = 0
            try:
                v = parts[8]
                if v.isdigit():
                    last_handshake = int(v)
            except (ValueError, IndexError):
                pass

            peers_data.append({
                "public_key": public_key,
                "endpoint": endpoint,
                "transfer_rx": transfer_rx,
                "transfer_tx": transfer_tx,
                "last_handshake": last_handshake,
            })
        except (ValueError, IndexError) as e:
            print(f"Parse error on line: {line[:120]}: {e}", file=sys.stderr)
            continue
    return peers_data

# ─── Сборщик трафика ───────────────────────────────────────────────────
_last_seen_handshakes: Dict[str, int] = {}

def collect_traffic_snapshots():
    """Собирает снимки трафика и логирует handshakes."""
    awg_data = parse_awg_dump()
    if not awg_data:
        return

    conn = get_db()
    now_ts = int(datetime.utcnow().timestamp())

    for peer_info in awg_data:
        pubkey = peer_info["public_key"]
        # Найти peer в БД
        row = conn.execute(
            "SELECT id, download_bytes, upload_bytes FROM peers WHERE public_key=?",
            (pubkey,)
        ).fetchone()
        if not row:
            continue
        peer_id, old_dl, old_ul = row["id"], row["download_bytes"], row["upload_bytes"]

        # Текущие значения
        cur_dl = peer_info["transfer_rx"]
        cur_ul = peer_info["transfer_tx"]

        # Если счётчики сбросились (peer перезапустился) — фиксируем снимок
        if cur_dl < old_dl or cur_ul < old_ul:
            # Пишем снимок со старыми значениями перед сбросом
            conn.execute(
                "INSERT INTO traffic_snapshots (peer_id, download_bytes, upload_bytes) VALUES (?,?,?)",
                (peer_id, old_dl, old_ul)
            )
            # Обновляем total
            conn.execute(
                "UPDATE peers SET download_bytes=?, upload_bytes=? WHERE id=?",
                (cur_dl, cur_ul, peer_id)
            )
        else:
            # Если есть новый трафик — пишем снимок
            if cur_dl > old_dl or cur_ul > old_ul:
                conn.execute(
                    "INSERT INTO traffic_snapshots (peer_id, download_bytes, upload_bytes) VALUES (?,?,?)",
                    (peer_id, cur_dl, cur_ul)
                )
            # Обновляем последние значения
            conn.execute(
                "UPDATE peers SET download_bytes=?, upload_bytes=? WHERE id=?",
                (cur_dl, cur_ul, peer_id)
            )

        # Логирование handshake и endpoint
        if peer_info["endpoint"]:
            # Обновляем last_endpoint в БД при любом изменении
            conn.execute(
                "UPDATE peers SET last_endpoint=? WHERE id=? AND (last_endpoint IS NULL OR last_endpoint != ?)",
                (peer_info["endpoint"], peer_id, peer_info["endpoint"])
            )
        if peer_info["last_handshake"] > 0:
            last_hs = peer_info["last_handshake"]
            prev_hs = _last_seen_handshakes.get(pubkey, 0)
            if last_hs != prev_hs and last_hs > prev_hs:
                # Новый handshake
                endpoint = peer_info["endpoint"]
                hs_time = datetime.fromtimestamp(last_hs).isoformat()
                conn.execute(
                    "INSERT INTO connection_log (peer_id, event_type, endpoint, timestamp) VALUES (?,?,?,?)",
                    (peer_id, "handshake", endpoint, hs_time)
                )
                conn.execute(
                    "UPDATE peers SET last_seen=?, last_endpoint=? WHERE id=?",
                    (hs_time, endpoint, peer_id)
                )
                _last_seen_handshakes[pubkey] = last_hs
                # Уведомление с rate limiting
                if notify.should_notify_handshake(peer_id):
                    peer_name_row = conn.execute("SELECT name, server_id FROM peers WHERE id=?", (peer_id,)).fetchone()
                    peer_name = peer_name_row["name"] if peer_name_row else "?"
                    peer_srv_id = peer_name_row["server_id"] if peer_name_row else 0
                    notify.notify("handshake", f"🔗 <b>{peer_name}</b> подключился с {endpoint}",
                                  peer_id=peer_id, peer_name=peer_name, endpoint=endpoint)
                    notify.log_notification("handshake", peer_id=peer_id)
                    realtime.ws_manager.broadcast_async({
                        "event": "handshake", "peer": peer_name,
                        "server_id": peer_srv_id, "endpoint": endpoint
                    })

    conn.commit()
    conn.close()

def traffic_threshold_check_loop():
    """Проверяет превышение порога трафика каждым peer'ом раз в час."""
    print("[traffic-threshold] started, interval=3600s")
    import time
    while True:
        try:
            conn = get_db()
            rows = conn.execute("""
                SELECT p.id, p.name, p.download_bytes, p.upload_bytes,
                       COALESCE(up.notify_traffic_threshold, 0) AS notify_traffic_threshold,
                       COALESCE(up.traffic_threshold_gb, 100) AS traffic_threshold_gb
                FROM peers p
                LEFT JOIN user_prefs up ON up.user = 'admin'
            """).fetchall()
            conn.close()
            for r in rows:
                threshold_gb = r["traffic_threshold_gb"] or 100
                if not r["notify_traffic_threshold"]:
                    continue
                total_gb = (r["download_bytes"] + r["upload_bytes"]) / (1024**3)
                if total_gb >= threshold_gb:
                    # Уведомляем не чаще раза в сутки
                    conn = get_db()
                    recent = conn.execute(
                        "SELECT COUNT(*) FROM notification_log WHERE event='traffic_threshold' AND peer_id=? AND timestamp > datetime('now', '-24 hours')",
                        (r["id"],)
                    ).fetchone()[0]
                    conn.close()
                    if recent == 0:
                        notify.notify("traffic_threshold",
                            f"📊 <b>{r['name']}</b>: {total_gb:.1f} GB превысил порог {threshold_gb} GB",
                            peer_id=r["id"], peer_name=r["name"], total_gb=round(total_gb, 2))
                        notify.log_notification("traffic_threshold", peer_id=r["id"])
        except Exception as e:
            print(f"[traffic-threshold] error: {e}", file=sys.stderr)
        time.sleep(3600)


def traffic_collector_loop():
    """Фоновый поток сбора трафика."""
    print(f"[traffic-collector] started, interval={TRAFFIC_COLLECT_INTERVAL}s")
    while True:
        try:
            collect_traffic_snapshots()
        except Exception as e:
            print(f"[traffic-collector] error: {e}", file=sys.stderr)
        import time
        time.sleep(TRAFFIC_COLLECT_INTERVAL)

# ─── App ───────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    ensure_admin()
    notify.ensure_notification_tables()
    realtime.ws_manager.set_loop(asyncio.get_running_loop())
    # Фоновые потоки
    t1 = threading.Thread(target=traffic_collector_loop, daemon=True)
    t1.start()
    t2 = threading.Thread(target=traffic_threshold_check_loop, daemon=True)
    t2.start()
    yield

app = FastAPI(title="AmneziaWG Panel", version=APP_VERSION, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Self-update helpers ───────────────────────────────────────────────
def _github_latest_release() -> dict:
    url = f"https://api.github.com/repos/{PANEL_REPO}/releases/latest"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "amnezia-panel-updater"}
    r = requests.get(url, headers=headers, timeout=8)
    if r.status_code == 404:
        return {"available": False, "error": "latest release not found", "repo": PANEL_REPO}
    r.raise_for_status()
    data = r.json()
    latest = (data.get("tag_name") or "").lstrip("v")
    return {
        "available": bool(latest),
        "repo": PANEL_REPO,
        "current_version": APP_VERSION,
        "latest_version": latest,
        "tag_name": data.get("tag_name"),
        "name": data.get("name"),
        "html_url": data.get("html_url"),
        "published_at": data.get("published_at"),
        "body": data.get("body") or "",
        "update_available": is_newer_version(latest, APP_VERSION),
        "image": PANEL_IMAGE,
    }


def _run_update_command(target_version: str):
    UPDATE_STATE.update({
        "running": True,
        "status": "running",
        "started_at": datetime.utcnow().isoformat() + "Z",
        "finished_at": None,
        "target_version": target_version,
        "log": "",
    })
    try:
        cmd = [PANEL_UPDATE_COMMAND]
        if not Path(PANEL_UPDATE_COMMAND).exists():
            raise RuntimeError(f"Update command not found: {PANEL_UPDATE_COMMAND}")
        env = os.environ.copy()
        env.update({
            "PANEL_TARGET_VERSION": target_version,
            "PANEL_IMAGE": PANEL_IMAGE,
            "PANEL_CONTAINER_NAME": env.get("PANEL_CONTAINER_NAME", "awg-panel"),
        })
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=PANEL_UPDATE_TIMEOUT,
            env=env,
            check=False,
        )
        output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        UPDATE_STATE["log"] = output[-12000:]
        if proc.returncode == 0:
            UPDATE_STATE["status"] = "completed"
        else:
            UPDATE_STATE["status"] = "failed"
            UPDATE_STATE["returncode"] = proc.returncode
    except Exception as e:
        UPDATE_STATE["status"] = "failed"
        UPDATE_STATE["log"] = str(e)
    finally:
        UPDATE_STATE["running"] = False
        UPDATE_STATE["finished_at"] = datetime.utcnow().isoformat() + "Z"

# ─── Routes ────────────────────────────────────────────────────────────

@app.get("/api/update/check")
def update_check(_: str = Depends(verify_token)):
    """Compare current app version with latest GitHub release."""
    try:
        data = _github_latest_release()
        data["state"] = UPDATE_STATE
        return data
    except requests.RequestException as e:
        return {
            "available": False,
            "repo": PANEL_REPO,
            "current_version": APP_VERSION,
            "latest_version": None,
            "update_available": False,
            "error": str(e),
            "state": UPDATE_STATE,
        }


@app.post("/api/update/apply")
def update_apply(user: str = Depends(verify_token)):
    """Start self-update in background. The command is fixed by env/script, not user input."""
    if UPDATE_STATE.get("running"):
        raise HTTPException(409, "Update already running")
    data = _github_latest_release()
    if not data.get("update_available"):
        return {"started": False, "message": "Already up to date", "release": data, "state": UPDATE_STATE}
    target = data.get("latest_version") or data.get("tag_name") or "latest"
    t = threading.Thread(target=_run_update_command, args=(target,), daemon=True)
    t.start()
    _write_audit(user, "self_update_start", target, json.dumps({"repo": PANEL_REPO, "image": PANEL_IMAGE}))
    return {"started": True, "target_version": target, "state": UPDATE_STATE}


@app.get("/api/update/status")
def update_status(_: str = Depends(verify_token)):
    return {"state": UPDATE_STATE, "current_version": APP_VERSION, "image": PANEL_IMAGE, "repo": PANEL_REPO}

@app.post("/api/login")
def login(req: LoginRequest):
    conn = get_db()
    row = conn.execute("SELECT password_hash FROM admins WHERE username=?", (req.username,)).fetchone()
    conn.close()
    if not row or not check_password(req.password, row["password_hash"]):
        raise HTTPException(401, "Invalid credentials")
    return {"token": create_token(req.username)}

@app.get("/api/peers", response_model=List[PeerResponse])
def list_peers(server_id: Optional[int] = None, _: str = Depends(verify_token)):
    conn = get_db()
    if server_id is not None:
        rows = conn.execute("SELECT * FROM peers WHERE server_id=? ORDER BY id", (server_id,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM peers ORDER BY id").fetchall()
    conn.close()
    result = []
    for r in rows:
        result.append({
            "id": r["id"],
            "name": r["name"],
            "public_key": r["public_key"],
            "ip_address": r["ip_address"],
            "enabled": bool(r["enabled"]),
            "created_at": str(r["created_at"]),
            "last_seen": str(r["last_seen"]) if r["last_seen"] else None,
            "last_endpoint": r["last_endpoint"],
            "download_bytes": r["download_bytes"],
            "upload_bytes": r["upload_bytes"],
        })
    return result

@app.post("/api/peers", response_model=PeerWithConfig)
def create_peer(req: PeerCreate, server_id: int = 0, user: str = Depends(verify_token)):
    server_row = _resolve_server(server_id)
    conn = get_db()
    if conn.execute("SELECT 1 FROM peers WHERE server_id=? AND name=?", (server_id, req.name)).fetchone():
        conn.close()
        raise HTTPException(400, "Name already exists on this server")
    priv, pub, psk = generate_keys()
    existing = conn.execute("SELECT ip_address FROM peers WHERE server_id=?", (server_id,)).fetchall()
    used = {r["ip_address"] for r in existing}
    ip = None
    for i in range(2, 255):
        candidate = f"10.8.1.{i}"
        if candidate not in used:
            ip = candidate
            break
    if not ip:
        conn.close()
        raise HTTPException(500, "No available IPs")

    conn.execute(
        "INSERT INTO peers (server_id, name, public_key, private_key, preshared_key, ip_address) VALUES (?,?,?,?,?,?)",
        (server_id, req.name, pub, priv, psk, ip)
    )
    conn.commit()
    conn.close()

    # Создаём peer на сервере (локально или удалённо)
    remote_ok = create_remote_peer(
        server_row, req.name, ip, pub, priv, psk
    )
    if not remote_ok:
        # Если не получилось на сервере — откатываем БД
        conn = get_db()
        conn.execute("DELETE FROM peers WHERE server_id=? AND name=?", (server_id, req.name))
        conn.commit()
        conn.close()
        raise HTTPException(500, "Failed to create peer on target server")

    server_pubkey = get_server_pubkey(server_row) or get_server_public_key()
    endpoint = server_row.get("endpoint") or os.getenv("AWG_ENDPOINT", "vpn.example.com:51820")
    config = gen_client_config_remote(priv, ip, psk, server_pubkey, endpoint)
    qr_b64 = make_qr(config)

    _write_audit(user, "create_peer", f"server={server_row.get('name')}/peer={req.name}")
    notify.notify("peer_added", f"➕ Новый клиент <b>{req.name}</b> на сервере {server_row.get('name')}",
                  peer_name=req.name, server=server_row.get('name'))
    realtime.ws_manager.broadcast_async({
        "event": "peer_added", "peer": req.name,
        "server": server_row.get('name'), "server_id": server_id
    })

    return {
        "id": 0, "name": req.name, "public_key": pub, "ip_address": ip,
        "enabled": True, "created_at": datetime.utcnow().isoformat(),
        "last_seen": None, "last_endpoint": None,
        "download_bytes": 0, "upload_bytes": 0,
        "config": config, "qr_code": qr_b64
    }

@app.delete("/api/peers/{peer_id}")
def delete_peer(peer_id: int, user: str = Depends(verify_token)):
    conn = get_db()
    row = conn.execute("SELECT * FROM peers WHERE id=?", (peer_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Peer not found")
    peer = dict(row)
    server_row = _resolve_server(peer["server_id"])
    conn.execute("DELETE FROM peers WHERE id=?", (peer_id,))
    conn.commit()
    conn.close()
    delete_remote_peer(server_row, peer["public_key"])
    _write_audit(user, "delete_peer", f"server={server_row.get('name')}/peer={peer['name']}")
    notify.notify("peer_deleted", f"🗑 Удалён клиент <b>{peer['name']}</b>",
                  peer_name=peer['name'], server=server_row.get('name'))
    realtime.ws_manager.broadcast_async({
        "event": "peer_deleted", "peer": peer['name'],
        "server": server_row.get('name'), "server_id": peer.get('server_id', 0)
    })
    return {"deleted": True}

@app.patch("/api/peers/{peer_id}/toggle")
def toggle_peer(peer_id: int, _: str = Depends(verify_token)):
    conn = get_db()
    conn.execute("UPDATE peers SET enabled = NOT enabled WHERE id=?", (peer_id,))
    conn.commit()
    row = conn.execute("SELECT enabled FROM peers WHERE id=?", (peer_id,)).fetchone()
    conn.close()
    return {"enabled": bool(row["enabled"])}

@app.get("/api/traffic/summary")
def get_traffic_summary(_: str = Depends(verify_token)):
    """Суммарный трафик по всем peer'ам за текущий месяц."""
    conn = get_db()
    now = datetime.utcnow()
    year_month = now.strftime("%Y-%m")
    rows = conn.execute("""
        SELECT
            p.id, p.name,
            COALESCE((SELECT download_bytes FROM traffic_snapshots
                      WHERE peer_id = p.id
                        AND strftime('%Y-%m', timestamp) = ?
                      ORDER BY timestamp DESC LIMIT 1), 0) as month_dl,
            COALESCE((SELECT upload_bytes FROM traffic_snapshots
                      WHERE peer_id = p.id
                        AND strftime('%Y-%m', timestamp) = ?
                      ORDER BY timestamp DESC LIMIT 1), 0) as month_ul,
            p.download_bytes as total_dl,
            p.upload_bytes as total_ul,
            p.last_seen,
            p.last_endpoint
        FROM peers p
        ORDER BY p.name
    """, (year_month, year_month)).fetchall()
    conn.close()

    return {
        "year_month": year_month,
        "peers": [
            {
                "id": r["id"],
                "name": r["name"],
                "month_download": r["month_dl"],
                "month_upload": r["month_ul"],
                "total_download": r["total_dl"],
                "total_upload": r["total_ul"],
                "last_seen": r["last_seen"],
                "last_endpoint": r["last_endpoint"],
            }
            for r in rows
        ]
    }

@app.get("/api/traffic/peer/{peer_id}")
def get_traffic(peer_id: int, year: Optional[int] = None, _: str = Depends(verify_token)):
    """Трафик конкретного peer'а по месяцам."""
    conn = get_db()
    if year:
        rows = conn.execute("""
            SELECT
                CAST(strftime('%Y', timestamp) AS INTEGER) as year,
                CAST(strftime('%m', timestamp) AS INTEGER) as month,
                download_bytes,
                upload_bytes
            FROM traffic_snapshots
            WHERE peer_id = ? AND strftime('%Y', timestamp) = ?
            ORDER BY timestamp
        """, (peer_id, str(year))).fetchall()
    else:
        rows = conn.execute("""
            SELECT
                CAST(strftime('%Y', timestamp) AS INTEGER) as year,
                CAST(strftime('%m', timestamp) AS INTEGER) as month,
                download_bytes,
                upload_bytes
            FROM traffic_snapshots
            WHERE peer_id = ?
            ORDER BY timestamp
        """, (peer_id,)).fetchall()
    conn.close()

    monthly = {}
    for r in rows:
        key = (r["year"], r["month"])
        if key not in monthly:
            monthly[key] = {"year": r["year"], "month": r["month"],
                           "download_bytes": 0, "upload_bytes": 0, "snapshots": 0}
        monthly[key]["download_bytes"] = r["download_bytes"]
        monthly[key]["upload_bytes"] = r["upload_bytes"]
        monthly[key]["snapshots"] += 1

    result = []
    for key in sorted(monthly.keys()):
        m = monthly[key]
        result.append({
            "year": m["year"],
            "month": m["month"],
            "download_bytes": m["download_bytes"],
            "upload_bytes": m["upload_bytes"],
        })
    return {"peer_id": peer_id, "monthly": result}

@app.get("/api/connections")
def get_connections(limit: int = 100, peer_id: Optional[int] = None,
                    _: str = Depends(verify_token)):
    """Лог подключений."""
    conn = get_db()
    if peer_id:
        rows = conn.execute("""
            SELECT cl.id, cl.peer_id, p.name, cl.event_type, cl.endpoint, cl.timestamp
            FROM connection_log cl
            JOIN peers p ON p.id = cl.peer_id
            WHERE cl.peer_id = ?
            ORDER BY cl.timestamp DESC
            LIMIT ?
        """, (peer_id, limit)).fetchall()
    else:
        rows = conn.execute("""
            SELECT cl.id, cl.peer_id, p.name, cl.event_type, cl.endpoint, cl.timestamp
            FROM connection_log cl
            JOIN peers p ON p.id = cl.peer_id
            ORDER BY cl.timestamp DESC
            LIMIT ?
        """, (limit,)).fetchall()
    conn.close()
    return {
        "logs": [
            {
                "id": r["id"],
                "peer_id": r["peer_id"],
                "peer_name": r["name"],
                "event_type": r["event_type"],
                "endpoint": r["endpoint"],
                "timestamp": str(r["timestamp"]),
            }
            for r in rows
        ]
    }

@app.post("/api/collect")
def manual_collect(_: str = Depends(verify_token)):
    """Ручной запуск сбора трафика и логирования."""
    collect_traffic_snapshots()
    return {"status": "ok", "collected": True}

# ─── Multi-server: helpers ─────────────────────────────────────────────
def _server_to_response(row) -> dict:
    # row может быть sqlite3.Row (БД) или dict (локальный сервер)
    def g(k, default=None):
        try: return row[k]
        except (KeyError, IndexError): return default
    return {
        "id": g("id"),
        "name": g("name"),
        "host": g("host"),
        "port": g("port", 22),
        "username": g("username"),
        "auth_type": g("auth_type", "password"),
        "amnezia_container": g("amnezia_container", "awg-tunnel"),
        "amnezia_iface": g("amnezia_iface", "awg0"),
        "listen_port": g("listen_port", 51820),
        "endpoint": g("endpoint"),
        "notes": g("notes"),
        "status": g("status", "unknown"),
        "last_check": str(g("last_check")) if g("last_check") else None,
        "created_at": str(g("created_at") or ""),
        "has_password": bool(g("password")),
        "has_key": bool(g("key_path")) or bool(g("key_data")),
    }

def _write_audit(user: str, action: str, target: str = None, details: str = None):
    conn = get_db()
    conn.execute(
        "INSERT INTO audit_log (user, action, target, details) VALUES (?,?,?,?)",
        (user, action, target, details)
    )
    conn.commit()
    conn.close()

def _resolve_server(server_id: int) -> dict:
    """Возвращает dict с расшифрованными credentials (для SSH-клиента)."""
    if server_id == 0:
        return {
            "id": 0,
            "name": "local",
            "host": "127.0.0.1",
            "port": 22,
            "username": "root",
            "auth_type": "password",
            "password": None,
            "key_path": None,
            "key_data": None,
            "amnezia_container": "awg-tunnel",
            "amnezia_iface": "awg0",
            "listen_port": int(os.getenv("AWG_LISTEN_PORT", "51820")),
            "endpoint": os.getenv("AWG_ENDPOINT"),
            "notes": "Локальный сервер (эта панель)",
            "status": "local",
            "last_check": None,
        }
    conn = get_db()
    row = conn.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, f"Server {server_id} not found")
    d = dict(row)
    d["password"] = crypto.decrypt(d.get("password"))
    d["key_data"] = crypto.decrypt(d.get("key_data"))
    return d


# ─── Server endpoints ─────────────────────────────────────────────────
@app.get("/api/servers", response_model=List[ServerResponse])
def list_servers(_: str = Depends(verify_token)):
    conn = get_db()
    rows = conn.execute("SELECT * FROM servers ORDER BY id").fetchall()
    conn.close()
    result = [_server_to_response(r) for r in rows]
    result.insert(0, _server_to_response(_resolve_server(0)))
    return result

@app.post("/api/servers", response_model=ServerResponse)
def create_server(req: ServerCreate, user: str = Depends(verify_token)):
    conn = get_db()
    if conn.execute("SELECT 1 FROM servers WHERE name=?", (req.name,)).fetchone():
        conn.close()
        raise HTTPException(400, "Server name already exists")
    enc_password = crypto.encrypt(req.password) if req.password else None
    enc_key_data = crypto.encrypt(req.key_data) if req.key_data else None
    cur = conn.execute(
        "INSERT INTO servers (name, host, port, username, auth_type, password, key_path, key_data, amnezia_container, amnezia_iface, listen_port, endpoint, notes, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'unknown')",
        (req.name, req.host, req.port, req.username, req.auth_type, enc_password, req.key_path, enc_key_data, req.amnezia_container, req.amnezia_iface, req.listen_port, req.endpoint, req.notes)
    )
    server_id = cur.lastrowid
    conn.commit()
    row = conn.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone()
    conn.close()
    _write_audit(user, "create_server", req.name, f"host={req.host}:{req.port}")
    return _server_to_response(row)

@app.get("/api/servers/{server_id}", response_model=ServerResponse)
def get_server(server_id: int, _: str = Depends(verify_token)):
    row = _resolve_server(server_id)
    return _server_to_response(row)

@app.put("/api/servers/{server_id}", response_model=ServerResponse)
def update_server(server_id: int, req: ServerUpdate, user: str = Depends(verify_token)):
    if server_id == 0:
        raise HTTPException(400, "Cannot edit local server")
    conn = get_db()
    row = conn.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Server not found")
    updates = {k: v for k, v in req.dict(exclude_unset=True).items() if v is not None}
    if not updates:
        conn.close()
        return _server_to_response(row)
    # Шифруем чувствительные поля при обновлении
    if "password" in updates and updates["password"]:
        updates["password"] = crypto.encrypt(updates["password"])
    if "key_data" in updates and updates["key_data"]:
        updates["key_data"] = crypto.encrypt(updates["key_data"])
    set_clause = ", ".join(f"{k}=?" for k in updates.keys())
    values = list(updates.values()) + [server_id]
    conn.execute(f"UPDATE servers SET {set_clause} WHERE id=?", values)
    conn.commit()
    row = conn.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone()
    conn.close()
    _write_audit(user, "update_server", row["name"], f"fields={list(updates.keys())}")
    return _server_to_response(row)

@app.delete("/api/servers/{server_id}")
def delete_server(server_id: int, user: str = Depends(verify_token)):
    if server_id == 0:
        raise HTTPException(400, "Cannot delete local server")
    conn = get_db()
    row = conn.execute("SELECT name FROM servers WHERE id=?", (server_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Server not found")
    conn.execute("DELETE FROM peers WHERE server_id=?", (server_id,))
    conn.execute("DELETE FROM servers WHERE id=?", (server_id,))
    conn.commit()
    conn.close()
    _write_audit(user, "delete_server", row["name"])
    return {"deleted": True}


@app.get("/api/local/health", response_model=ServerHealthResponse)
def local_health(_: str = Depends(verify_token)):
    """Read-only health for the local AWG container shown in the header."""
    row = _resolve_server(0)
    health = check_server_health(row)
    health.setdefault("peer_count", 0)
    health.setdefault("total_download", 0)
    health.setdefault("total_upload", 0)
    if health.get("ok"):
        try:
            peers = get_server_peers(row)
            health["peer_count"] = len(peers)
            health["total_download"] = sum(p.get("transfer_rx", 0) for p in peers)
            health["total_upload"] = sum(p.get("transfer_tx", 0) for p in peers)
        except Exception as e:
            health["error"] = str(e)
    return health

@app.post("/api/servers/{server_id}/test", response_model=ServerHealthResponse)
def test_server(server_id: int, user: str = Depends(verify_token)):
    row = _resolve_server(server_id)
    health = check_server_health(row)
    health["peer_count"] = 0
    health["total_download"] = 0
    health["total_upload"] = 0
    if health.get("ok"):
        try:
            peers = get_server_peers(row)
            health["peer_count"] = len(peers)
            health["total_download"] = sum(p["transfer_rx"] for p in peers)
            health["total_upload"] = sum(p["transfer_tx"] for p in peers)
        except Exception as e:
            health["error"] = str(e)
    try:
        conn = get_db()
        status_text = "ok" if health.get("ok") else "error"
        conn.execute(
            "UPDATE servers SET status=?, last_check=CURRENT_TIMESTAMP WHERE id=?",
            (status_text, server_id if server_id > 0 else 0)
        )
        if server_id > 0:
            conn.commit()
        conn.close()
    except Exception:
        pass
    _write_audit(user, "test_server", row.get("name"), f"ok={health.get('ok')}")
    if server_id > 0:
        srv_name = row.get("name", "?")
        if not health.get("ok"):
            notify.notify("server_down",
                f"🔴 Сервер <b>{srv_name}</b> недоступен: {health.get('error', '?')}",
                server=srv_name, error=health.get("error", ""))
        else:
            notify.notify("server_up",
                f"🟢 Сервер <b>{srv_name}</b> снова онлайн", server=srv_name)
    return health

@app.post("/api/servers/{server_id}/action")
def server_action(server_id: int, body: dict, user: str = Depends(verify_token)):
    action = body.get("action", "")
    if action not in ("start", "stop", "restart"):
        raise HTTPException(400, "action must be: start | stop | restart")
    row = _resolve_server(server_id)
    ok = service_action(row, action)
    _write_audit(user, f"service_{action}", row.get("name"), f"ok={ok}")
    return {"ok": ok, "action": action}

@app.get("/api/servers/{server_id}/peers")
def list_remote_peers(server_id: int, _: str = Depends(verify_token)):
    row = _resolve_server(server_id)
    return {"server_id": server_id, "peers": get_server_peers(row)}


@app.get("/api/aggregate/stats")
def aggregate_stats(_: str = Depends(verify_token)):
    """Сводная статистика по всем серверам."""
    return aggregate.collect_all_stats()


@app.get("/api/aggregate/servers")
def aggregate_servers(_: str = Depends(verify_token)):
    """Список серверов с краткой статистикой."""
    stats = aggregate.collect_all_stats()
    return {"servers": stats.get("servers", []), "ok_count": stats.get("ok_count", 0)}


@app.get("/api/audit")
def get_audit_log(limit: int = 100, _: str = Depends(verify_token)):
    """Журнал действий администраторов."""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, user, action, target, details, timestamp FROM audit_log ORDER BY timestamp DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return {"log": [dict(r) for r in rows]}


@app.get("/api/discover/scan")
def discover_scan(subnet: str = "172.16.0.0/24", _: str = Depends(verify_token)):
    """Сканирует подсеть и ищет потенциальные AmneziaWG/панели."""
    return discover.scan_subnet(subnet)


@app.post("/api/notifications/channels")
def add_channel(body: dict, user: str = Depends(verify_token)):
    """Создаёт канал уведомлений.

    Body: {name, type: "telegram"|"webhook", config: {bot_token,chat_id}|{url}}
    """
    name = body.get("name", "")
    ch_type = body.get("type", "")
    config = body.get("config", {})
    if not name or ch_type not in ("telegram", "webhook", "email") or not config:
        raise HTTPException(400, "Invalid payload")
    channel_id = notify.add_channel(name, ch_type, config)
    _write_audit(user, "add_channel", name, ch_type)
    notify.notify("test", f"✅ Канал <b>{name}</b> настроен!", type=ch_type)
    return {"id": channel_id, "name": name, "type": ch_type}


@app.get("/api/notifications/channels")
def list_channels(_: str = Depends(verify_token)):
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name, type, config, enabled, created_at FROM notification_channels ORDER BY id"
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        cfg = json.loads(r["config"])
        # Скрываем секреты
        if r["type"] == "telegram":
            cfg = {"bot_token": cfg.get("bot_token", "")[:10] + "...", "chat_id": cfg.get("chat_id")}
        elif r["type"] == "webhook":
            cfg = {"url": cfg.get("url", "")}
        result.append({
            "id": r["id"], "name": r["name"], "type": r["type"],
            "config": cfg, "enabled": bool(r["enabled"]),
            "created_at": str(r["created_at"])
        })
    return result


@app.delete("/api/notifications/channels/{channel_id}")
def del_channel(channel_id: int, user: str = Depends(verify_token)):
    if notify.delete_channel(channel_id):
        _write_audit(user, "del_channel", str(channel_id))
        return {"deleted": True}
    raise HTTPException(404, "Not found")


@app.patch("/api/notifications/channels/{channel_id}")
def upd_channel(channel_id: int, body: dict, user: str = Depends(verify_token)):
    kwargs = {}
    if "name" in body: kwargs["name"] = body["name"]
    if "enabled" in body: kwargs["enabled"] = body["enabled"]
    if "config" in body: kwargs["config"] = body["config"]
    ok = notify.update_channel(channel_id, **kwargs)
    if ok:
        _write_audit(user, "upd_channel", str(channel_id))
    return {"updated": ok}


@app.get("/api/notifications/log")
def notif_log(limit: int = 50, _: str = Depends(verify_token)):
    conn = get_db()
    rows = conn.execute(
        "SELECT id, event, peer_id, server_id, sent, timestamp FROM notification_log ORDER BY timestamp DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return {"log": [dict(r) for r in rows]}


# ─── WebSocket ────────────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await realtime.ws_manager.connect(ws)
    try:
        while True:
            # Просто держим соединение активным, читаем любые сообщения от клиента
            data = await ws.receive_text()
            # Клиент может прислать {"action": "ping"} для проверки связи
            try:
                msg = json.loads(data)
                if msg.get("action") == "ping":
                    await ws.send_text(json.dumps({"event": "pong"}))
            except Exception:
                pass
    except WebSocketDisconnect:
        await realtime.ws_manager.disconnect(ws)
    except Exception:
        await realtime.ws_manager.disconnect(ws)


# ─── Per-server notification preferences ──────────────────────────────
@app.get("/api/servers/{server_id}/prefs")
def get_server_prefs(server_id: int, _: str = Depends(verify_token)):
    """Получает настройки уведомлений для сервера."""
    conn = get_db()
    row = conn.execute("SELECT * FROM server_prefs WHERE server_id=?", (server_id,)).fetchone()
    conn.close()
    if row:
        return dict(row)
    return {"server_id": server_id, "notify_handshake": 1, "notify_traffic": 1, "traffic_threshold_gb": 100}


@app.put("/api/servers/{server_id}/prefs")
def set_server_prefs(server_id: int, body: dict, _: str = Depends(verify_token)):
    conn = get_db()
    existing = conn.execute("SELECT id FROM server_prefs WHERE server_id=?", (server_id,)).fetchone()
    notify_handshake = int(bool(body.get("notify_handshake", True)))
    notify_traffic = int(bool(body.get("notify_traffic", False)))
    traffic_threshold_gb = int(body.get("traffic_threshold_gb", 100))
    if existing:
        conn.execute(
            "UPDATE server_prefs SET notify_handshake=?, notify_traffic=?, traffic_threshold_gb=? WHERE server_id=?",
            (notify_handshake, notify_traffic, traffic_threshold_gb, server_id)
        )
    else:
        conn.execute(
            "INSERT INTO server_prefs (server_id, notify_handshake, notify_traffic, traffic_threshold_gb) VALUES (?,?,?,?)",
            (server_id, notify_handshake, notify_traffic, traffic_threshold_gb)
        )
    conn.commit()
    conn.close()
    return {"server_id": server_id, "notify_handshake": bool(notify_handshake),
            "notify_traffic": bool(notify_traffic), "traffic_threshold_gb": traffic_threshold_gb}


def make_qr(config: str) -> str:
    img = qrcode.make(config)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()

# ─── Static (frontend) ────────────────────────────────────────────────
class NoCacheStaticFiles(StaticFiles):
    """StaticFiles с отключенным кешированием для разработки."""
    async def __call__(self, scope, receive, send):
        async def wrapped_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                # Cache-Control: no-cache, no-store, must-revalidate
                headers.append((b"cache-control", b"no-cache, no-store, must-revalidate"))
                headers.append((b"pragma", b"no-cache"))
                headers.append((b"expires", b"0"))
                message["headers"] = headers
            await send(message)
        return await super().__call__(scope, receive, wrapped_send)

static_dir = Path("/app/static")
if static_dir.exists():
    app.mount("/", NoCacheStaticFiles(directory=static_dir, html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PANEL_PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
