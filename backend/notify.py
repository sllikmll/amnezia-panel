"""
notify.py — система уведомлений (Telegram bot + webhooks).

Поддерживает события:
- peer_added / peer_deleted
- server_up / server_down
- handshake (новое подключение клиента)
- traffic_threshold (превышен порог трафика)

Каналы:
- Telegram: bot_token + chat_id
- Webhook: HTTP POST с JSON
"""

import os
import json
import time
import threading
import sqlite3
import smtplib
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from typing import Optional, List, Dict
from pathlib import Path

def _get_db():
    from main import get_db
    return get_db()


# ─── Каналы ────────────────────────────────────────────────────────────
def send_telegram(bot_token: str, chat_id: str, text: str) -> bool:
    """Отправляет сообщение в Telegram."""
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10
        )
        return r.status_code == 200
    except Exception as e:
        print(f"telegram error: {e}", file=__import__("sys").stderr)
        return False


def send_email(cfg: Dict, subject: str, body: str) -> bool:
    """Отправляет email через SMTP с поддержкой TLS/STARTTLS.

    cfg: {host, port, user, password, from_addr, to_addrs, use_tls (0|1|starttls)}
    """
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = cfg.get("from_addr", cfg.get("user", ""))
        msg["To"] = ", ".join(cfg.get("to_addrs", []))
        # HTML body
        html_body = f"""<html><body>
<div style="font-family: -apple-system, sans-serif; max-width: 600px; margin: 20px auto; padding: 20px; background: #1a2a3a; color: #e0e6ed; border-radius: 10px;">
  <h2 style="color: #4ade80; margin-top: 0;">{subject}</h2>
  <div style="background: #0f1923; padding: 15px; border-radius: 5px; white-space: pre-wrap;">{body}</div>
  <p style="color: #888; font-size: 12px; margin-top: 15px;">AmneziaWG Panel • {datetime.utcnow().isoformat()}</p>
</div></body></html>"""
        msg.attach(MIMEText(html_body, "html"))
        msg.attach(MIMEText(body, "plain"))

        port = int(cfg.get("port", 587))
        use_tls = cfg.get("use_tls", "starttls")
        timeout = int(cfg.get("timeout", 10))

        if use_tls == "ssl":
            server = smtplib.SMTP_SSL(cfg["host"], port, timeout=timeout)
        else:
            server = smtplib.SMTP(cfg["host"], port, timeout=timeout)

        if use_tls == "starttls":
            server.starttls()
        if cfg.get("user") and cfg.get("password"):
            server.login(cfg["user"], cfg["password"])
        server.sendmail(msg["From"], cfg.get("to_addrs", []), msg.as_string())
        server.quit()
        return True
    except Exception as e:
        print(f"email error: {e}", file=__import__("sys").stderr)
        return False


def send_webhook(url: str, payload: Dict) -> bool:
    """Отправляет POST на webhook URL."""
    try:
        r = requests.post(url, json=payload, timeout=10)
        return r.status_code < 400
    except Exception as e:
        print(f"webhook error: {e}", file=__import__("sys").stderr)
        return False


# ─── Настройки каналов ─────────────────────────────────────────────────
def get_channels() -> List[Dict]:
    """Возвращает активные каналы уведомлений из БД."""
    conn = _get_db()
    rows = conn.execute("SELECT * FROM notification_channels WHERE enabled=1").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_channel_by_id(channel_id: int) -> Optional[Dict]:
    conn = _get_db()
    row = conn.execute("SELECT * FROM notification_channels WHERE id=?", (channel_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def add_channel(name: str, channel_type: str, config: Dict) -> int:
    """Добавляет канал. config: {bot_token, chat_id} для telegram; {url} для webhook."""
    conn = _get_db()
    cur = conn.execute(
        "INSERT INTO notification_channels (name, type, config, enabled) VALUES (?,?,?,1)",
        (name, channel_type, json.dumps(config))
    )
    channel_id = cur.lastrowid
    conn.commit()
    conn.close()
    return channel_id


def delete_channel(channel_id: int) -> bool:
    conn = _get_db()
    conn.execute("DELETE FROM notification_channels WHERE id=?", (channel_id,))
    conn.commit()
    conn.close()
    return True


def update_channel(channel_id: int, **kwargs) -> bool:
    """Обновляет параметры канала."""
    conn = _get_db()
    sets = []
    values = []
    if "name" in kwargs:
        sets.append("name=?")
        values.append(kwargs["name"])
    if "enabled" in kwargs:
        sets.append("enabled=?")
        values.append(1 if kwargs["enabled"] else 0)
    if "config" in kwargs:
        sets.append("config=?")
        values.append(json.dumps(kwargs["config"]))
    if not sets:
        conn.close()
        return False
    values.append(channel_id)
    conn.execute(f"UPDATE notification_channels SET {', '.join(sets)} WHERE id=?", values)
    conn.commit()
    conn.close()
    return True


# ─── Отправка уведомлений ─────────────────────────────────────────────
def notify(event_type: str, message: str, **context):
    """Отправляет уведомление во все активные каналы.

    Проверяет preferences каналов (какие события включены).
    """
    channels = get_channels()
    if not channels:
        return

    # Полный текст + контекст в JSON
    full_msg = f"<b>{event_type}</b>\n{message}"
    payload = {
        "event": event_type,
        "message": message,
        "timestamp": datetime.utcnow().isoformat(),
        **context
    }

    for ch in channels:
        sent = False
        try:
            prefs = json.loads(ch.get("prefs") or "{}")
            if event_type in prefs and not prefs[event_type]:
                continue  # событие отключено

            cfg = json.loads(ch["config"])

            if ch["type"] == "telegram":
                sent = send_telegram(
                    cfg.get("bot_token"),
                    cfg.get("chat_id"),
                    full_msg
                )
            elif ch["type"] == "webhook":
                sent = send_webhook(cfg.get("url"), payload)
            elif ch["type"] == "email":
                subject = f"AmneziaWG Panel: {event_type}"
                sent = send_email(cfg, subject, message)

            log_notification(event_type, sent=sent)
        except Exception as e:
            print(f"notify error channel {ch.get('id')}: {e}", file=__import__("sys").stderr)
            log_notification(event_type, sent=False)


# ─── Правила (events для handshake, traffic threshold) ────────────────
def should_notify_handshake(peer_id: int) -> bool:
    """Проверяет, стоит ли уведомлять о новом handshake (rate limiting)."""
    conn = _get_db()
    # Не чаще 1 раза в 5 минут для одного peer'а
    recent = conn.execute(
        "SELECT COUNT(*) FROM notification_log WHERE peer_id=? AND event='handshake' AND timestamp > datetime('now', '-5 minutes')",
        (peer_id,)
    ).fetchone()[0]
    conn.close()
    return recent == 0


def log_notification(event: str, peer_id: Optional[int] = None, server_id: Optional[int] = None, sent: bool = True):
    conn = _get_db()
    conn.execute(
        "INSERT INTO notification_log (event, peer_id, server_id, sent) VALUES (?,?,?,?)",
        (event, peer_id, server_id, sent)
    )
    conn.commit()
    conn.close()


# ─── Инициализация таблиц (вызывается из main) ────────────────────────
def ensure_notification_tables():
    conn = _get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS notification_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            type TEXT NOT NULL,  -- 'telegram' | 'webhook'
            config TEXT NOT NULL,
            prefs TEXT,  -- JSON: {event: bool}
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
        CREATE INDEX IF NOT EXISTS idx_notif_log_time
            ON notification_log(timestamp);
    """)
    conn.commit()
    conn.close()
