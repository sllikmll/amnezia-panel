"""
multi_server.py — управление удалёнными AmneziaWG серверами через SSH.

Каждый сервер может быть:
- Локальным (id=0, операции напрямую через docker exec)
- Удалённым (id>0, операции через paramiko SSH)

Удалённый сервер требует:
- host, port, username
- authentication: пароль ИЛИ путь к приватному ключу
- amnezia_container: имя docker контейнера с awg (default: awg-tunnel)
- amnezia_iface: имя интерфейса (default: awg0)
- listen_port: для генерации конфигов клиента
"""

import os
import io
import base64
import json
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import List, Optional, Dict, Tuple

import paramiko


# ─── SSH executor ──────────────────────────────────────────────────────
class SSHClient:
    """Обёртка для SSH-подключения с keepalive."""

    def __init__(self, host: str, port: int, username: str,
                 password: Optional[str] = None,
                 key_path: Optional[str] = None,
                 key_data: Optional[str] = None,
                 timeout: int = 10):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.key_path = key_path
        self.key_data = key_data
        self.timeout = timeout
        self._client: Optional[paramiko.SSHClient] = None
        self._lock = threading.Lock()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def connect(self):
        with self._lock:
            if self._client is not None:
                return
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            kwargs = {
                "hostname": self.host,
                "port": self.port,
                "username": self.username,
                "timeout": self.timeout,
                "allow_agent": False,
                "look_for_keys": False,
            }
            if self.password:
                kwargs["password"] = self.password
            if self.key_path:
                kwargs["key_filename"] = self.key_path
            elif self.key_data:
                # Загружаем ключ из строки
                key_file = io.StringIO(self.key_data)
                pkey = None
                for key_cls in (paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey):
                    try:
                        key_file.seek(0)
                        pkey = key_cls.from_private_key(key_file)
                        break
                    except Exception:
                        continue
                if pkey is None:
                    # Может быть с паролем
                    key_file.seek(0)
                    pkey = paramiko.RSAKey.from_private_key(key_file)
                kwargs["pkey"] = pkey
            client.connect(**kwargs)
            self._client = client

    def close(self):
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None

    def exec(self, command: str, timeout: int = 30, input: Optional[str] = None) -> Tuple[int, str, str]:
        """Выполняет команду, возвращает (rc, stdout, stderr)."""
        with self._lock:
            if self._client is None:
                self.connect()
            stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout)
            if input is not None:
                stdin.write(input)
                stdin.channel.shutdown_write()
            rc = stdout.channel.recv_exit_status()
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            return rc, out, err

    def file(self, path: str, mode: str = "r") -> "paramiko.SFTPFile":
        """Открывает файл через SFTP."""
        with self._lock:
            if self._client is None:
                self.connect()
            sftp = self._client.open_sftp()
            return sftp.file(path, mode=mode)


def _shell_quote(value: object) -> str:
    """Shell-quote values interpolated into remote docker commands."""
    return shlex.quote(str(value))


AWG_CONFIG_CANDIDATES = (
    "/opt/amnezia/awg/awg0.conf",
    "/opt/amnezia/state/amnezia-awg2/awg0.conf",
    "/opt/amnezia/state/amnezia-awg2-direct/awgdir0.conf",
    "/data/awg0.conf",
)
AWG_CLIENTS_DIR = "/opt/amnezia/clients"


def _container_shell(container: str, script: str) -> str:
    """Build a safely quoted shell command executed inside the AWG container."""
    return f"docker exec {_shell_quote(container)} sh -lc {_shell_quote(script)}"


def _config_discovery_script() -> str:
    candidates = " ".join(_shell_quote(path) for path in AWG_CONFIG_CANDIDATES)
    return (
        "CFG=''; "
        f"for f in {candidates}; do [ -f \"$f\" ] && {{ CFG=\"$f\"; break; }}; done; "
        "[ -n \"$CFG\" ]"
    )


def from_server_row(row: Dict) -> SSHClient:
    """Создаёт SSHClient из строки БД."""
    return SSHClient(
        host=row["host"],
        port=row.get("port", 22),
        username=row["username"],
        password=row.get("password"),
        key_path=row.get("key_path"),
        key_data=row.get("key_data"),
    )


# ─── Операции на сервере ───────────────────────────────────────────────
def check_server_health(row: Dict) -> Dict:
    """Проверяет состояние сервера и его awg-сервиса."""
    is_local = row.get("id") == 0

    try:
        if is_local:
            # Локальный сервер — используем docker
            r = subprocess.run(
                ["docker", "ps", "--filter", f"name={row.get('amnezia_container', 'awg-tunnel')}",
                 "--format", "{{.Names}} {{.Status}}"],
                capture_output=True, text=True, timeout=10
            )
            rc, out, err = r.returncode, r.stdout, r.stderr
            if rc == 0 and out.strip():
                container_status = "running"
            else:
                container_status = "not_found"
            # Проверим статус awg-go
            try:
                r2 = subprocess.run(
                    ["docker", "exec", row.get("amnezia_container", "awg-tunnel"),
                     "sh", "-lc", "AWG=$(command -v awg || echo /usr/local/bin/awg); $AWG show"],
                    capture_output=True, text=True, timeout=10
                )
                out2 = r2.stdout
                awg_status = "running" if "listening port" in out2 else "down"
                listen_port = ""
                if "listening port" in out2:
                    for line in out2.split("\n"):
                        if "listening port" in line:
                            listen_port = line.split(":")[-1].strip()
            except Exception:
                awg_status = "unknown"
                listen_port = ""
        else:
            # Удалённый сервер — через SSH
            with from_server_row(row) as ssh:
                # Проверяем docker
                container = row.get('amnezia_container', 'awg-tunnel')
                rc, out, err = ssh.exec(
                    f"docker ps --filter {_shell_quote('name=' + container)} --format '{{{{.Names}}}} {{{{.Status}}}}'",
                    timeout=15
                )
                container_status = "running" if rc == 0 and out.strip() else "not_found"
                # Проверяем awg
                rc2, out2, _ = ssh.exec(
                    f"docker exec {_shell_quote(container)} sh -lc 'AWG=$(command -v awg || echo /usr/local/bin/awg); $AWG show'",
                    timeout=15
                )
                awg_status = "running" if "listening port" in out2 else "down"
                listen_port = ""
                if "listening port" in out2:
                    for line in out2.split("\n"):
                        if "listening port" in line:
                            listen_port = line.split(":")[-1].strip()

        ok = container_status == "running" and awg_status == "running"
        return {
            "ok": ok,
            "container_status": container_status,
            "awg_status": awg_status,
            "listen_port": listen_port,
            "host": row["host"],
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "container_status": "error",
            "awg_status": "error",
            "host": row.get("host", "?"),
        }


def get_server_peers(row: Dict) -> List[Dict]:
    """Получает список peer'ов с сервера через awg show all dump."""
    is_local = row.get("id") == 0
    container = row.get("amnezia_container", "awg-tunnel")
    cmd = _container_shell(
        container,
        'AWG=$(command -v awg || echo /usr/bin/awg); "$AWG" show all dump',
    )

    try:
        if not is_local:
            with from_server_row(row) as ssh:
                rc, out, err = ssh.exec(cmd, timeout=15)
        else:
            r = subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=10)
            rc, out, err = r.returncode, r.stdout, r.stderr

        if rc != 0:
            return []

        return parse_remote_dump(out)
    except Exception as e:
        print(f"get_server_peers error: {e}", file=__import__("sys").stderr)
        return []


def parse_remote_dump(stdout: str) -> List[Dict]:
    """Парсит вывод awg show all dump (9 полей)."""
    peers = []
    for line in stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 9 or "/" not in parts[4]:
            continue
        if len(parts[1]) < 40 or "(none)" in parts[1]:
            continue
        try:
            peers.append({
                "public_key": parts[1],
                "endpoint": parts[3] if parts[3] != "(none)" else "",
                "transfer_rx": int(parts[6]) if parts[6].isdigit() else 0,
                "transfer_tx": int(parts[7]) if parts[7].isdigit() else 0,
                "last_handshake": int(parts[5]) if parts[5].isdigit() else 0,
                "allowed_ips": parts[4] if len(parts) > 4 else "",
            })
        except (ValueError, IndexError):
            continue
    return peers


def execute_on_server(row: Dict, command: str, timeout: int = 30) -> Tuple[int, str, str]:
    """Выполняет произвольную команду на сервере."""
    is_local = row.get("id") == 0
    if is_local:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return result.returncode, result.stdout, result.stderr
    else:
        with from_server_row(row) as ssh:
            return ssh.exec(command, timeout=timeout)


def get_server_pubkey(row: Dict) -> str:
    """Получает публичный ключ сервера AmneziaWG."""
    is_local = row.get("id") == 0
    container = row.get('amnezia_container', 'awg-tunnel')
    script = (
        _config_discovery_script()
        + "; AWG=$(command -v awg || echo /usr/bin/awg); "
          "KEY=$(sed -n 's/^[[:space:]]*PrivateKey[[:space:]]*=[[:space:]]*//p' \"$CFG\" | head -1); "
          "[ -n \"$KEY\" ] && printf '%s' \"$KEY\" | \"$AWG\" pubkey"
    )
    cmd = _container_shell(container, script)
    try:
        if is_local:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
            return result.stdout.strip()
        else:
            with from_server_row(row) as ssh:
                rc, out, _ = ssh.exec(cmd, timeout=10)
                return out.strip() if rc == 0 else ""
    except Exception:
        return ""


def create_remote_peer(row: Dict, name: str, ip_address: str,
                       public_key: str, private_key: str, psk: str,
                       client_config: Optional[str] = None) -> bool:
    """Создаёт peer на удалённом сервере и сохраняет importable client .conf."""
    conf = (
        f"\n# {name}\n"
        f"[Peer]\n"
        f"PublicKey = {public_key}\n"
        f"PresharedKey = {psk}\n"
        f"AllowedIPs = {ip_address}/32\n"
    )

    container = row.get("amnezia_container", "awg-tunnel")
    safe_name = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name) or "client"
    peer_b64 = base64.b64encode(conf.encode()).decode()
    client_b64 = base64.b64encode((client_config or "").encode()).decode()
    script = (
        "set -eu; " + _config_discovery_script() + "; "
        f"! grep -qF {_shell_quote('PublicKey = ' + public_key)} \"$CFG\"; "
        "cp \"$CFG\" \"$CFG.bak-panel-$(date +%Y%m%d-%H%M%S)\"; "
        f"printf '%s' {_shell_quote(peer_b64)} | base64 -d >> \"$CFG\"; "
        f"mkdir -p {_shell_quote(AWG_CLIENTS_DIR)}; "
        f"printf '%s' {_shell_quote(client_b64)} | base64 -d > "
        f"{_shell_quote(AWG_CLIENTS_DIR + '/' + safe_name + '.conf')}; "
        f"chmod 600 {_shell_quote(AWG_CLIENTS_DIR + '/' + safe_name + '.conf')} \"$CFG\""
    )
    try:
        rc, _, err = execute_on_server(row, _container_shell(container, script), timeout=20)
        if rc != 0:
            print(f"persistent peer write failed: {err}", file=__import__("sys").stderr)
            return False
        rc, _, err = execute_on_server(row, f"docker restart {_shell_quote(container)}", timeout=40)
        if rc != 0:
            print(f"docker restart failed: {err}", file=__import__("sys").stderr)
            return False
        return True
    except Exception as e:
        print(f"create_remote_peer error: {e}", file=__import__("sys").stderr)
        return False


def delete_remote_peer(row: Dict, public_key: str) -> bool:
    """Удаляет peer на удалённом сервере."""
    container = row.get("amnezia_container", "awg-tunnel")
    script = (
        "set -eu; " + _config_discovery_script() + "; "
        "cp \"$CFG\" \"$CFG.bak-panel-$(date +%Y%m%d-%H%M%S)\"; "
        f"awk -v key={_shell_quote(public_key)} 'BEGIN {{ RS=\"\"; ORS=\"\\n\\n\" }} "
        "index($0, \"PublicKey = \" key) == 0 { print }' \"$CFG\" > \"$CFG.tmp\"; "
        "mv \"$CFG.tmp\" \"$CFG\"; chmod 600 \"$CFG\"; "
        "AWG=$(command -v awg || echo /usr/bin/awg); "
        f"for f in {_shell_quote(AWG_CLIENTS_DIR)}/*.conf; do [ -f \"$f\" ] || continue; "
        "PRIV=$(sed -n 's/^[[:space:]]*PrivateKey[[:space:]]*=[[:space:]]*//p' \"$f\" | head -1); "
        "PUB=$(printf '%s' \"$PRIV\" | \"$AWG\" pubkey 2>/dev/null || true); "
        f"[ \"$PUB\" = {_shell_quote(public_key)} ] && rm -f \"$f\" || true; done"
    )
    try:
        rc, _, _ = execute_on_server(row, _container_shell(container, script), timeout=20)
        if rc != 0:
            return False
        rc, _, _ = execute_on_server(row, f"docker restart {_shell_quote(container)}", timeout=40)
        return rc == 0
    except Exception as e:
        return False


def service_action(row: Dict, action: str) -> bool:
    """start/stop/restart awg-сервиса на сервере."""
    is_local = row.get("id") == 0
    container = row.get("amnezia_container", "awg-tunnel")
    cmd = f"docker {action} {_shell_quote(container)}"
    try:
        if is_local:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        else:
            with from_server_row(row) as ssh:
                rc, out, err = ssh.exec(cmd, timeout=30)
                return rc == 0
        return result.returncode == 0
    except Exception:
        return False
