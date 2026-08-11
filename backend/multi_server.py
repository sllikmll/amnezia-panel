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
                     "/usr/local/bin/awg", "show"],
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
                    f"docker exec {_shell_quote(container)} /usr/local/bin/awg show",
                    timeout=15
                )
                awg_status = "running" if "listening port" in out2 else "down"
                listen_port = ""
                if "listening port" in out2:
                    for line in out2.split("\n"):
                        if "listening port" in line:
                            listen_port = line.split(":")[-1].strip()

        return {
            "ok": True,
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
    cmd = ["docker", "exec", row.get("amnezia_container", "awg-tunnel"),
           "/usr/local/bin/awg", "show", "all", "dump"]

    try:
        if not is_local:
            with from_server_row(row) as ssh:
                rc, out, err = ssh.exec(
                    " ".join(f"'{c}'" if " " in c else c for c in cmd),
                    timeout=15
                )
        else:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
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
        if len(parts) < 9:
            continue
        if len(parts[1]) < 40 or "(none)" in parts[1]:
            continue
        try:
            peers.append({
                "public_key": parts[1],
                "endpoint": parts[3] if parts[3] != "(none)" else "",
                "transfer_rx": int(parts[5]) if parts[5].isdigit() else 0,
                "transfer_tx": int(parts[6]) if parts[6].isdigit() else 0,
                "last_handshake": int(parts[8]) if parts[8].isdigit() else 0,
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
    cmd = f"docker exec {_shell_quote(container)} cat /data/server.pub"
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
                       public_key: str, private_key: str, psk: str) -> bool:
    """Создаёт peer на удалённом сервере через awg syncconf."""
    is_local = row.get("id") == 0
    conf = (
        f"[Peer]\n"
        f"PublicKey = {public_key}\n"
        f"PresharedKey = {psk}\n"
        f"AllowedIPs = {ip_address}/32\n"
    )

    container = row.get("amnezia_container", "awg-tunnel")
    try:
        if is_local:
            # Локально — пишем в файл и awg syncconf через stdin
            proc = subprocess.run(
                ["docker", "exec", "-i", container, "/usr/local/bin/awg", "syncconf", "awg0"],
                input=conf, text=True, capture_output=True, timeout=10
            )
        else:
            with from_server_row(row) as ssh:
                # Пишем conf в файл через heredoc
                cmd = f"""docker exec -i {_shell_quote(container)} /usr/local/bin/awg syncconf awg0 << 'EOF'
{conf}
EOF"""
                rc, out, err = ssh.exec(cmd, timeout=15)
                if rc != 0:
                    print(f"syncconf failed: {err}", file=__import__("sys").stderr)
                    return False
        return True
    except Exception as e:
        print(f"create_remote_peer error: {e}", file=__import__("sys").stderr)
        return False


def delete_remote_peer(row: Dict, public_key: str) -> bool:
    """Удаляет peer на удалённом сервере."""
    is_local = row.get("id") == 0
    container = row.get("amnezia_container", "awg-tunnel")
    cmd_input = f"public_key = {public_key}\nremove\n"
    try:
        if is_local:
            proc = subprocess.run(
                ["docker", "exec", "-i", container, "/usr/local/bin/awg", "syncconf", "awg0"],
                input=cmd_input, text=True, capture_output=True, timeout=10
            )
        else:
            with from_server_row(row) as ssh:
                cmd = f"docker exec -i {_shell_quote(container)} /usr/local/bin/awg syncconf awg0"
                rc, out, err = ssh.exec(cmd, input=cmd_input, timeout=15)
                if rc != 0:
                    return False
        return True
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
