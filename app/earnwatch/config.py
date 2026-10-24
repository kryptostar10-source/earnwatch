"""Konfiguration aus Umgebungsvariablen / Docker-Secrets."""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

log = logging.getLogger("earnwatch")

os.umask(0o077)  # Datenbank & Schlüssel nur für den App-Benutzer lesbar

DATA_DIR = Path(os.environ.get("EW_DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "earnwatch.db"
SECURE_COOKIES = os.environ.get("EW_SECURE_COOKIES", "true").lower() in ("1", "true", "yes")
INTERVAL_MIN_DEFAULT = int(os.environ.get("EW_INTERVAL_MIN", "60"))
SESSION_HOURS = int(os.environ.get("EW_SESSION_HOURS", "12"))
REMEMBER_DAYS = int(os.environ.get("EW_REMEMBER_DAYS", "30"))  # "Angemeldet bleiben"
TZ = os.environ.get("TZ", "Europe/Berlin")


def _read_secret(env_name: str) -> str | None:
    """Liest ein Secret aus <NAME>_FILE (Docker-Secret) oder <NAME>."""
    file_path = os.environ.get(f"{env_name}_FILE")
    if file_path and Path(file_path).is_file():
        value = Path(file_path).read_text().strip()
        if value:
            return value
    value = os.environ.get(env_name, "").strip()
    return value or None


def _persisted_key(name: str, factory) -> bytes:
    """Schlüssel aus /data laden oder einmalig mit Rechten 0600 erzeugen."""
    path = DATA_DIR / name
    if path.is_file():
        return path.read_bytes().strip()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    key = factory()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    return key


def encryption_key() -> bytes:
    from cryptography.fernet import Fernet

    explicit = _read_secret("EW_ENCRYPTION_KEY")
    if explicit:
        return explicit.encode()
    return _persisted_key(".fernet.key", Fernet.generate_key)


def session_secret() -> str:
    explicit = _read_secret("EW_SESSION_SECRET")
    if explicit:
        return explicit
    return _persisted_key(".session.key", lambda: secrets.token_urlsafe(48).encode()).decode()


def setup_token() -> str:
    """Token, das beim allerersten Konto-Anlegen eingegeben werden muss."""
    explicit = _read_secret("EW_SETUP_TOKEN")
    if explicit:
        return explicit
    token = _persisted_key(".setup.token", lambda: secrets.token_urlsafe(18).encode()).decode()
    return token
