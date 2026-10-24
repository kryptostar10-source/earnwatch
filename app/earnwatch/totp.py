"""Zwei-Faktor-Anmeldung: TOTP nach RFC 6238 (kompatibel mit allen Authenticator-Apps)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
import urllib.parse

STEP = 30
DIGITS = 6


def new_secret() -> str:
    """160-Bit-Zufallsschlüssel, Base32 (so wie Authenticator-Apps ihn erwarten)."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret_b32: str) -> bytes:
    s = secret_b32.strip().replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def code_at(secret_b32: str, counter: int, digits: int = DIGITS, algo=hashlib.sha1) -> str:
    mac = hmac.new(_key(secret_b32), struct.pack(">Q", counter), algo).digest()
    off = mac[-1] & 0x0F
    num = struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF
    return str(num % (10 ** digits)).zfill(digits)


def verify(secret_b32: str, code: str, last_counter: int = 0, now: float | None = None) -> int | None:
    """Prüft einen 6-stelligen Code (±30 s Toleranz).

    Gibt den verwendeten Zeitschritt zurück (zum Speichern, damit derselbe Code
    nicht ein zweites Mal benutzt werden kann) oder None.
    """
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != DIGITS:
        return None
    t = int((now if now is not None else time.time()) // STEP)
    for c in (t - 1, t, t + 1):
        if c <= last_counter:
            continue  # schon benutzt -> Wiederholungsangriff verhindern
        if hmac.compare_digest(code_at(secret_b32, c), code):
            return c
    return None


def otpauth_uri(secret_b32: str, account: str, issuer: str = "EarnWatch") -> str:
    label = urllib.parse.quote(f"{issuer}:{account}")
    q = urllib.parse.urlencode({"secret": secret_b32, "issuer": issuer, "digits": DIGITS, "period": STEP})
    return f"otpauth://totp/{label}?{q}"


def qr_svg(data: str) -> str | None:
    """QR-Code als SVG (Bibliothek segno). None, falls nicht installiert."""
    try:
        import segno
    except ImportError:
        return None
    return segno.make(data, error="m", micro=False).svg_inline(
        scale=5, border=3, dark="#0a0d11", light="#ffffff")


# --- Wiederherstellungscodes ----------------------------------------------
_ALPH = "abcdefghjkmnpqrstuvwxyz23456789"  # ohne 0/o/1/l/i – leicht abzuschreiben


def new_recovery_codes(n: int = 8) -> list[str]:
    out = []
    for _ in range(n):
        raw = "".join(secrets.choice(_ALPH) for _ in range(12))
        out.append(f"{raw[:4]}-{raw[4:8]}-{raw[8:]}")
    return out


def hash_recovery(code: str) -> str:
    norm = "".join(ch for ch in code.lower() if ch.isalnum())
    return hashlib.sha256(("earnwatch-recovery:" + norm).encode()).hexdigest()


def use_recovery(code: str, hashes: list[str]) -> list[str] | None:
    """Gibt die Liste ohne den benutzten Code zurück, oder None wenn ungültig."""
    h = hash_recovery(code)
    for i, x in enumerate(hashes):
        if hmac.compare_digest(h, x):
            return hashes[:i] + hashes[i + 1:]
    return None
