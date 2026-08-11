"""
aggregate.py — агрегация статистики по всем серверам.

Собирает данные локально + с удалённых серверов (через SSH/awg show).
Используется фоновым потоком и API.
"""

import time
import threading
import concurrent.futures
from typing import Dict, List, Optional

from multi_server import (
    get_server_peers,
    check_server_health,
)


def _collect_server_stats(server_row: dict) -> Optional[Dict]:
    """Собирает статистику одного сервера. None при ошибке."""
    sid = server_row.get("id", 0)
    try:
        health = check_server_health(server_row)
        if not health.get("ok"):
            return {
                "server_id": sid,
                "name": server_row.get("name"),
                "ok": False,
                "error": health.get("error", "unknown"),
            }
        peers = get_server_peers(server_row)
        total_dl = sum(p.get("transfer_rx", 0) for p in peers)
        total_ul = sum(p.get("transfer_tx", 0) for p in peers)
        return {
            "server_id": sid,
            "name": server_row.get("name"),
            "ok": True,
            "container_status": health.get("container_status"),
            "awg_status": health.get("awg_status"),
            "listen_port": health.get("listen_port"),
            "peer_count": len(peers),
            "total_download": total_dl,
            "total_upload": total_ul,
            "active_peers": sum(1 for p in peers if p.get("last_handshake", 0) > 0),
        }
    except Exception as e:
        return {
            "server_id": sid,
            "name": server_row.get("name"),
            "ok": False,
            "error": str(e),
        }


def get_db_servers() -> List[dict]:
    """Возвращает список всех серверов (локальный + удалённые) с расшифрованными credentials."""
    import os
    from main import get_db  # late import to avoid circular
    # Локальный сервер
    result = [{
        "id": 0, "name": "local", "host": "127.0.0.1", "port": 22,
        "username": "root", "auth_type": "password",
        "password": None, "key_path": None, "key_data": None,
        "amnezia_container": "awg-tunnel", "amnezia_iface": "awg0",
        "listen_port": int(os.getenv("AWG_LISTEN_PORT", "51820")),
        "endpoint": os.getenv("AWG_ENDPOINT"),
    }]
    conn = get_db()
    rows = conn.execute("SELECT * FROM servers ORDER BY id").fetchall()
    conn.close()
    import crypto
    for r in rows:
        d = dict(r)
        d["password"] = crypto.decrypt(d.get("password"))
        d["key_data"] = crypto.decrypt(d.get("key_data"))
        result.append(d)
    return result


def collect_all_stats(timeout: int = 10) -> Dict:
    """Параллельно собирает статистику по всем серверам."""
    servers = get_db_servers()
    result = {"servers": [], "total_download": 0, "total_upload": 0,
              "total_peers": 0, "active_peers": 0, "ok_count": 0}

    # Параллельный сбор (до 4 потоков)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(_collect_server_stats, srv): srv
            for srv in servers
        }
        for fut in concurrent.futures.as_completed(futures, timeout=timeout):
            try:
                stats = fut.result(timeout=timeout)
            except Exception as e:
                srv = futures[fut]
                stats = {"server_id": srv.get("id"), "name": srv.get("name"),
                         "ok": False, "error": str(e)}
            if stats:
                result["servers"].append(stats)
                if stats.get("ok"):
                    result["ok_count"] += 1
                    result["total_download"] += stats.get("total_download", 0)
                    result["total_upload"] += stats.get("total_upload", 0)
                    result["total_peers"] += stats.get("peer_count", 0)
                    result["active_peers"] += stats.get("active_peers", 0)

    result["servers"].sort(key=lambda s: s.get("server_id", 999))
    return result
