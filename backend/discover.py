"""
discover.py — авто-обнаружение серверов AmneziaWG/панелей в подсети.

Сканирует подсеть через ping + nmap, проверяет:
- SSH (порт 22)
- AmneziaWG (порт 51820/UDP)
- Наша панель (порт 8888/TCP — POST /api/login)
"""

import os
import re
import ipaddress
import socket
import subprocess
import threading
import concurrent.futures
from typing import List, Dict, Optional


def _ping(ip: str, timeout: int = 1) -> bool:
    """Проверяет хост через ping."""
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout), ip],
            capture_output=True, timeout=timeout + 1
        )
        return result.returncode == 0
    except Exception:
        return False


def _check_port(ip: str, port: int, timeout: int = 2) -> bool:
    """Проверяет TCP-порт."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


def _check_awg(ip: str, port: int = 51820, timeout: int = 2) -> bool:
    """Проверяет что UDP-порт открыт (AmneziaWG)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            # UDP — просто отправляем пакет, без проверки ответа
            s.sendto(b"\x01\x00\x00\x00", (ip, port))
            return True
    except Exception:
        return False


def _check_panel(ip: str, port: int = 8888, timeout: int = 2) -> Dict:
    """Проверяет наша ли это панель."""
    try:
        import httpx
        with httpx.Client(timeout=timeout) as client:
            r = client.post(
                f"http://{ip}:{port}/api/login",
                json={"username": "admin", "password": "wrong"}
            )
            if r.status_code in (401, 422):
                return {"is_panel": True, "version": r.headers.get("server", "unknown")}
    except Exception:
        pass
    return {"is_panel": False}


def _scan_host(ip: str) -> Optional[Dict]:
    """Сканирует один хост."""
    if not _ping(ip):
        return None

    result = {
        "ip": ip,
        "hostname": "",
        "ssh": _check_port(ip, 22),
        "panel": None,
        "amneziawg": _check_awg(ip, 51820),
        "score": 0,  # насколько вероятно что это amneziawg-сервер
    }

    # Hostname
    try:
        result["hostname"] = socket.gethostbyaddr(ip)[0]
    except Exception:
        pass

    # Проверяем нашу панель
    for port in (8888, 8080, 8889, 8890, 80, 443):
        if _check_port(ip, port):
            panel_info = _check_panel(ip, port)
            if panel_info.get("is_panel"):
                result["panel"] = {"port": port, **panel_info}
                result["score"] += 50
                break

    # Scoring
    if result["ssh"]:
        result["score"] += 30
    if result["amneziawg"]:
        result["score"] += 40
    if result["hostname"]:
        if "awg" in result["hostname"].lower():
            result["score"] += 20
        if "vpn" in result["hostname"].lower():
            result["score"] += 20
        if "amnezia" in result["hostname"].lower():
            result["score"] += 30

    return result


def scan_subnet(subnet: str = "172.16.0.0/24", max_workers: int = 32) -> Dict:
    """Сканирует подсеть параллельно.

    Возвращает:
    {
        "subnet": "172.16.0.0/24",
        "scanned": 254,
        "alive": N,
        "candidates": [host_dicts sorted by score]
    }
    """
    try:
        network = ipaddress.ip_network(subnet, strict=False)
    except ValueError as e:
        return {"error": str(e)}

    # Генерируем список IP
    if network.num_addresses > 1024:
        return {"error": f"Subnet too large: {network.num_addresses} addresses (max 1024)"}

    ips = [str(ip) for ip in network.hosts()]

    result = {
        "subnet": subnet,
        "scanned": len(ips),
        "alive": 0,
        "candidates": [],
    }

    # Параллельный ping + port scan
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_scan_host, ip): ip for ip in ips}
        for fut in concurrent.futures.as_completed(futures, timeout=120):
            try:
                host = fut.result(timeout=10)
            except Exception:
                continue
            if host is None:
                continue
            result["alive"] += 1
            result["candidates"].append(host)

    # Сортировка по score
    result["candidates"].sort(key=lambda h: (-h.get("score", 0), h["ip"]))
    return result
