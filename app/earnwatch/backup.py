"""Umzug per Backup-Datei (z. B. Proxmox → Umbrel).

Die Datei ist mit einem eigenen Backup-Passwort verschlüsselt:
scrypt (n=2^15, r=8, p=1) leitet den Schlüssel ab, AES-256-GCM verschlüsselt + schützt vor Änderungen.
Ohne das Passwort ist der Inhalt (Zugangsdaten, Passwort-Hash, 2FA-Geheimnis) nicht lesbar.

Einspielen geht nur, solange die Installation noch KEIN Konto hat (beim ersten Start).
Passkeys werden nicht übernommen – sie sind an die Adresse der alten Installation gebunden.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import zlib

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from . import crypto, db

FORMAT = "earnwatch-backup"
VERSION = 1
MAX_FILE = 30 * 1024 * 1024          # hochgeladene Datei
MAX_PLAIN = 200 * 1024 * 1024        # entpackt (Schutz vor Zip-Bomben)
KDF = {"n": 2 ** 15, "r": 8, "p": 1}

# Nur diese Einstellungen wandern mit. Gerätespezifisches (Container-Liste, letzter Abruf …) bleibt.
SETTINGS = {"achievements", "currency", "duino_rigs", "interval_min", "notify_events", "notify_summary_hour",
            "theme", "ui_style", "widget_token_hash", "widget_token_ts", "eur_rate", "eur_rate_ts"}
PF_RE = re.compile(r"^_?[a-z0-9]{1,32}$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
USER_RE = re.compile(r"^[A-Za-z0-9]{3,32}$")
HASH_RE = re.compile(r"^scrypt\$\d{1,7}\$\d{1,2}\$\d{1,2}\$[A-Za-z0-9+/=]{8,64}\$[A-Za-z0-9+/=]{20,128}$")


class BackupError(ValueError):
    pass


def _key(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(password.encode())


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


# --------------------------------------------------------------------------
def export(password: str) -> bytes:
    with db.conn() as c:
        users = []
        for u in c.execute("SELECT * FROM users ORDER BY id"):
            sec = crypto.decrypt(u["totp_secret"]) if u["totp_secret"] else None
            users.append({"username": u["username"], "pw_hash": u["pw_hash"], "created": u["created"],
                          "totp": sec.get("s") if sec else None, "totp_last": u["totp_last"] or 0,
                          "recovery": u["recovery"]})
        creds = {}
        for r in c.execute("SELECT platform, blob FROM credentials"):
            d = crypto.decrypt(r["blob"])
            if d is not None:
                creds[r["platform"]] = d
        snaps = [[r["platform"], r["ts"], r["balance"], r["currency"]]
                 for r in c.execute("SELECT platform, ts, balance, currency FROM snapshots ORDER BY id")]
        daily = [[r["platform"], r["day"], r["amount"]] for r in c.execute("SELECT * FROM imported_daily")]
        settings = {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM settings")
                    if r["key"] in SETTINGS}
    payload = {"users": users, "credentials": creds, "snapshots": snaps, "imported_daily": daily,
               "settings": settings}
    plain = zlib.compress(json.dumps(payload, separators=(",", ":")).encode(), 6)
    salt, nonce = os.urandom(16), os.urandom(12)
    head = {"format": FORMAT, "v": VERSION, "created": int(time.time()), "kdf": "scrypt", **KDF,
            "salt": _b64(salt), "nonce": _b64(nonce)}
    aad = json.dumps(head, sort_keys=True).encode()
    ct = AESGCM(_key(password, salt, **KDF)).encrypt(nonce, plain, aad)
    return json.dumps({**head, "data": _b64(ct)}).encode()


# --------------------------------------------------------------------------
def _decrypt(raw: bytes, password: str) -> dict:
    try:
        doc = json.loads(raw)
        if not isinstance(doc, dict) or doc.get("format") != FORMAT or doc.get("v") != VERSION:
            raise BackupError("Das ist keine EarnWatch-Backup-Datei.")
        n, r, p = int(doc["n"]), int(doc["r"]), int(doc["p"])
        if (n, r, p) != (KDF["n"], KDF["r"], KDF["p"]):   # keine fremden (teuren) Parameter annehmen
            raise BackupError("Unbekanntes Backup-Format.")
        head = {k: doc[k] for k in ("format", "v", "created", "kdf", "n", "r", "p", "salt", "nonce")}
        salt, nonce, ct = (base64.b64decode(doc[k], validate=True) for k in ("salt", "nonce", "data"))
    except (ValueError, KeyError, TypeError) as e:
        if isinstance(e, BackupError):
            raise
        raise BackupError("Die Datei ist beschädigt oder keine EarnWatch-Backup-Datei.") from None
    try:
        plain = AESGCM(_key(password, salt, n, r, p)).decrypt(nonce, ct, json.dumps(head, sort_keys=True).encode())
    except (InvalidTag, ValueError):
        raise BackupError("Backup-Passwort ist falsch (oder die Datei wurde verändert).") from None
    d = zlib.decompressobj()
    data = d.decompress(plain, MAX_PLAIN)
    if d.unconsumed_tail:
        raise BackupError("Backup ist zu groß.")
    try:
        out = json.loads(data)
    except ValueError:
        raise BackupError("Backup ist beschädigt.") from None
    if not isinstance(out, dict):
        raise BackupError("Backup ist beschädigt.")
    return out


def _clean(p: dict) -> dict:
    """Alles streng prüfen – auch wenn die Datei verschlüsselt war."""
    users = []
    for u in (p.get("users") or [])[:10]:
        name, h = str(u.get("username", "")), str(u.get("pw_hash", ""))
        if not USER_RE.match(name) or not HASH_RE.match(h):
            continue
        totp = u.get("totp")
        totp = str(totp) if totp and re.fullmatch(r"[A-Z2-7]{16,64}", str(totp)) else None
        rec = u.get("recovery")
        if rec is not None:
            try:
                lst = json.loads(rec)
                rec = json.dumps([str(x)[:200] for x in lst[:20]]) if isinstance(lst, list) else None
            except (ValueError, TypeError):
                rec = None
        users.append((name, h, int(u.get("created") or time.time()),
                      crypto.encrypt({"s": totp}) if totp else None,
                      max(0, int(u.get("totp_last") or 0)) if totp else 0, rec if totp else None))
    if not users:
        raise BackupError("Im Backup ist kein Konto enthalten.")
    creds = {}
    for pf, d in (p.get("credentials") or {}).items():
        if PF_RE.match(str(pf)) and isinstance(d, dict) and len(json.dumps(d)) < 20000:
            creds[str(pf)] = d
    snaps = []
    for s in p.get("snapshots") or []:
        try:
            pf, ts, bal, cur = str(s[0]), int(s[1]), float(s[2]), str(s[3])[:8]
        except (ValueError, TypeError, IndexError):
            continue
        if PF_RE.match(pf) and 0 < ts < 4_102_444_800 and abs(bal) < 1e12:
            snaps.append((pf, ts, bal, cur))
    daily = []
    for s in p.get("imported_daily") or []:
        try:
            pf, day, amt = str(s[0]), str(s[1]), float(s[2])
        except (ValueError, TypeError, IndexError):
            continue
        if PF_RE.match(pf) and DAY_RE.match(day) and abs(amt) < 1e12:
            daily.append((pf, day, amt))
    settings = {k: str(v)[:100000] for k, v in (p.get("settings") or {}).items() if k in SETTINGS}
    return {"users": users, "creds": creds, "snaps": snaps, "daily": daily, "settings": settings}


def restore(raw: bytes, password: str) -> dict:
    """Spielt ein Backup ein – nur in eine leere Installation. Gibt eine kleine Zusammenfassung zurück."""
    if len(raw) > MAX_FILE:
        raise BackupError("Datei ist zu groß.")
    data = _clean(_decrypt(raw, password))
    now = int(time.time())
    with db.conn() as c:
        if c.execute("SELECT COUNT(*) FROM users").fetchone()[0]:   # atomar unter derselben Sperre
            raise BackupError("Hier gibt es schon ein Konto – Backup kann nur beim ersten Start eingespielt werden.")
        c.executemany("INSERT INTO users(username, pw_hash, created, totp_secret, totp_last, recovery) "
                      "VALUES (?,?,?,?,?,?)", data["users"])
        c.executemany("INSERT OR REPLACE INTO credentials(platform, blob, updated) VALUES (?,?,?)",
                      [(pf, crypto.encrypt(d), now) for pf, d in data["creds"].items()])
        c.execute("DELETE FROM snapshots")
        c.executemany("INSERT INTO snapshots(platform, ts, balance, currency) VALUES (?,?,?,?)", data["snaps"])
        c.executemany("INSERT OR REPLACE INTO imported_daily(platform, day, amount) VALUES (?,?,?)", data["daily"])
        c.executemany("INSERT OR REPLACE INTO settings(key, value) VALUES (?,?)", list(data["settings"].items()))
    for pf, d in data["creds"].items():   # Ablaufdaten der Tokens neu merken
        db.remember_expiry(pf, d)
    return {"users": [u[0] for u in data["users"]], "platforms": len([k for k in data["creds"] if not k.startswith("_")]),
            "snapshots": len(data["snaps"])}
