"""Verschlüsselung der Zugangsdaten (Fernet = AES-128-CBC + HMAC-SHA256)."""
from __future__ import annotations

import json

from cryptography.fernet import Fernet, InvalidToken

from . import config

_fernet: Fernet | None = None


def _f() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(config.encryption_key())
    return _fernet


def encrypt(data: dict) -> bytes:
    return _f().encrypt(json.dumps(data).encode())


def decrypt(blob: bytes) -> dict | None:
    try:
        return json.loads(_f().decrypt(blob))
    except (InvalidToken, ValueError):
        return None
