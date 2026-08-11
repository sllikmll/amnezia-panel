"""
crypto.py — шифрование чувствительных данных в БД.

Использует Fernet (AES-128-CBC + HMAC-SHA256).
Мастер-ключ берётся из env PANEL_ENCRYPTION_KEY (Fernet.generate_key() для генерации).
Если ключ не задан — генерируется при первом запуске и пишется в /data/encryption.key
с правами 0o600.
"""

import os
import stat
import secrets
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken


_KEY_FILE = Path("/data/encryption.key")


def _load_or_create_key() -> bytes:
    """Загружает ключ из env или файла, или создаёт новый."""
    key = os.getenv("PANEL_ENCRYPTION_KEY")
    if key:
        return key.encode() if isinstance(key, str) else key

    if _KEY_FILE.exists():
        return _KEY_FILE.read_bytes()

    key = Fernet.generate_key()
    _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    _KEY_FILE.write_bytes(key)
    os.chmod(_KEY_FILE, 0o600)
    return key


# Singleton Fernet
_fernet = Fernet(_load_or_create_key())


def encrypt(plain: Optional[str]) -> Optional[str]:
    """Шифрует строку, возвращает base64-string. None/пустое -> None."""
    if not plain:
        return None
    return _fernet.encrypt(plain.encode()).decode()


def decrypt(cipher: Optional[str]) -> Optional[str]:
    """Расшифровывает. None -> None. Неверный токен -> None."""
    if not cipher:
        return None
    try:
        return _fernet.decrypt(cipher.encode()).decode()
    except (InvalidToken, ValueError):
        return None
