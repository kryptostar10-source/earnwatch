"""Nur-Lese-Spiegel: Eine Installation (z. B. Umbrel) zeigt die Daten einer anderen (z. B. Proxmox).

Sicherheit – unabhängig von HTTPS/Zertifikaten im Heimnetz:
* Quelle und Spiegel teilen einen geheimen 256-Bit-Schlüssel (einmal per Spiegel-Link übertragen).
* Jede Anfrage ist mit HMAC-SHA256 signiert (Zeitstempel + Einmal-Nonce → keine Wiederholung).
* Jede Antwort ist mit AES-256-GCM verschlüsselt und an die Nonce der Anfrage gebunden.
  Wer dazwischen sitzt, kann nichts lesen, nichts ändern und keine alten Antworten unterschieben.
* Übertragen werden NUR Anzeige-Daten (Kontostände, Abrufe, Kurse). Nie Zugangsdaten, Passwörter, 2FA.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import db

PREFIX = "ewmirror1:"
MAX_RESP = 60 * 1024 * 1024
MAX_PLAIN = 300 * 1024 * 1024
SNAP_BATCH = 100_000
SKEW = 300
# Anzeige-Einstellungen, die vom Original übernommen werden (keine Geheimnisse)
SETTING_RE = re.compile(r"^(eur_rate|eur_rate_ts|containers|last_collect_ts|achievements|duino_rigs|interval_min"
                        r"|price_[a-z0-9]{1,32}|info_[a-z0-9]{1,32}|cred_exp_[a-z0-9]{1,32})$")
PF_RE = re.compile(r"^[a-z0-9]{1,32}$")

_seen: dict[str, float] = {}
_seen_lock = threading.Lock()


class MirrorError(Exception):
    pass


def _keys(master: bytes) -> tuple[bytes, bytes]:
    return (hmac.new(master, b"earnwatch-mirror-auth", hashlib.sha256).digest(),
            hmac.new(master, b"earnwatch-mirror-enc", hashlib.sha256).digest())


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ==========================================================================
# Quelle (z. B. Proxmox)
# ==========================================================================
def source_key() -> bytes | None:
    d = db.load_credentials("_mirror_src")
    try:
        return _unb64u(d["k"]) if d and d.get("k") else None
    except (ValueError, KeyError):
        return None


def source_info() -> dict:
    d = db.load_credentials("_mirror_src") or {}
    return {"active": bool(d.get("k")), "created": int(d.get("created") or 0),
            "last_seen": int(db.get_setting("mirror_src_seen", "0") or 0)}


def create_source_link(base_url: str) -> str:
    key = secrets.token_bytes(32)
    db.save_credentials("_mirror_src", {"k": _b64u(key), "created": int(time.time())})
    db.set_setting("mirror_src_seen", "0")
    payload = json.dumps({"u": base_url, "k": _b64u(key)}, separators=(",", ":")).encode()
    return PREFIX + _b64u(payload)


def revoke_source() -> None:
    db.delete_credentials("_mirror_src")
    db.set_setting("mirror_src_seen", "0")


def _sig(auth: bytes, ts: str, nonce: str, since: str) -> str:
    return hmac.new(auth, f"{ts}|{nonce}|{since}".encode(), hashlib.sha256).hexdigest()


def check_request(headers) -> tuple[bytes, str, int] | None:
    """Prüft Signatur, Zeit und Nonce. Gibt (enc_key, nonce, since) oder None zurück."""
    master = source_key()
    if not master:
        return None
    ts, nonce, since, sig = (str(headers.get(h, "")) for h in ("x-ew-ts", "x-ew-nonce", "x-ew-since", "x-ew-sig"))
    if not (ts.isdigit() and since.isdigit() and re.fullmatch(r"[A-Za-z0-9_-]{16,64}", nonce)
            and re.fullmatch(r"[0-9a-f]{64}", sig)):
        return None
    auth, enc = _keys(master)
    if not hmac.compare_digest(_sig(auth, ts, nonce, since), sig):
        return None
    now = time.time()
    if abs(now - int(ts)) > SKEW:
        return None
    with _seen_lock:
        for k in [k for k, t in _seen.items() if t < now - 2 * SKEW]:
            del _seen[k]
        if nonce in _seen or len(_seen) > 10000:
            return None
        _seen[nonce] = now
    db.set_setting("mirror_src_seen", str(int(now)))
    return enc, nonce, int(since)


def build_response(enc: bytes, nonce: str, since: int) -> bytes:
    with db.conn() as c:
        snaps = c.execute("SELECT id, platform, ts, balance, currency FROM snapshots WHERE id>? ORDER BY id LIMIT ?",
                          (since, SNAP_BATCH)).fetchall()
        total_max = c.execute("SELECT COALESCE(MAX(id),0) FROM snapshots").fetchone()[0]
        daily = [[r["platform"], r["day"], r["amount"]] for r in c.execute("SELECT * FROM imported_daily")]
        runs = [[r["platform"], r["ts"], r["ok"], r["message"]] for r in c.execute(
            "SELECT platform, ts, ok, message FROM runs WHERE ts>? ORDER BY id", (int(time.time()) - 14 * 86400,))]
        settings = {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM settings")
                    if SETTING_RE.match(r["key"])}
        configured = {r["platform"]: r["updated"] for r in c.execute("SELECT platform, updated FROM credentials")
                      if PF_RE.match(r["platform"])}
    payload = {"t": int(time.time()), "max_id": total_max,
               "snapshots": [[r["id"], r["platform"], r["ts"], r["balance"], r["currency"]] for r in snaps],
               "imported_daily": daily, "runs": runs, "settings": settings, "configured": configured}
    plain = zlib.compress(json.dumps(payload, separators=(",", ":")).encode(), 6)
    iv = os.urandom(12)
    return iv + AESGCM(enc).encrypt(iv, plain, nonce.encode())


# ==========================================================================
# Spiegel (z. B. Umbrel)
# ==========================================================================
def parse_link(link: str) -> tuple[str, bytes]:
    link = (link or "").strip()
    if not link.startswith(PREFIX) or len(link) > 2000:
        raise MirrorError("Das ist kein Spiegel-Link. Er beginnt mit „ewmirror1:“.")
    try:
        d = json.loads(_unb64u(link[len(PREFIX):]))
        url, key = str(d["u"]), _unb64u(str(d["k"]))
    except (ValueError, KeyError, TypeError):
        raise MirrorError("Der Spiegel-Link ist unvollständig – bitte komplett kopieren.") from None
    if len(key) != 32:
        raise MirrorError("Der Spiegel-Link ist beschädigt.")
    return check_url(url), key


def check_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment \
            or len(url) > 300 or (u.path not in ("", "/")):
        raise MirrorError("Adresse bitte so angeben: https://192.168.1.50 (ohne Pfad).")
    return url


def mirror_config() -> dict | None:
    d = db.load_credentials("_mirror")
    return d if d and d.get("k") and d.get("u") else None


def enable(url: str, key: bytes) -> None:
    with db.conn() as c:   # frisch anfangen: keine alten/eigenen Werte mit Spiegel-Werten mischen
        c.execute("DELETE FROM snapshots")
        c.execute("DELETE FROM runs")
        c.execute("DELETE FROM imported_daily")
    db.save_credentials("_mirror", {"u": url, "k": _b64u(key)})
    db.set_setting("mirror_last_id", "0")
    db.set_setting("mirror_last_ok", "0")
    db.set_setting("mirror_error", "")


def disable() -> None:
    db.delete_credentials("_mirror")
    with db.conn() as c:
        c.execute("DELETE FROM snapshots")
        c.execute("DELETE FROM runs")
        c.execute("DELETE FROM imported_daily")
    for k in ("mirror_configured", "mirror_last_id", "mirror_last_ok", "mirror_error", "containers", "last_collect_ts"):
        db.set_setting(k, "")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):   # Weiterleitungen nicht folgen
        return None


def _fetch(url: str, master: bytes, since: int) -> dict:
    auth, enc = _keys(master)
    ts, nonce = str(int(time.time())), _b64u(secrets.token_bytes(18))
    req = urllib.request.Request(url + "/api/mirror", headers={
        "x-ew-ts": ts, "x-ew-nonce": nonce, "x-ew-since": str(since),
        "x-ew-sig": _sig(auth, ts, nonce, str(since)), "user-agent": "EarnWatch-Spiegel"})
    def _open(ctx):
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx), _NoRedirect())
        with opener.open(req, timeout=40) as r:
            return r.read(MAX_RESP + 1)
    try:
        try:   # zuerst normal prüfen (z. B. Tailscale-Adresse mit echtem Zertifikat)
            raw = _open(ssl.create_default_context())
        except urllib.error.URLError as e:
            if not isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError):
                raise
            # Selbst ausgestelltes Heimnetz-Zertifikat (Caddy intern, erneuert sich ständig, nicht prüfbar).
            # Vom Nutzer so gewählt: Vertraulichkeit + Echtheit kommen hier von HMAC-Signatur + AES-256-GCM oben.
            lax = ssl.create_default_context()
            lax.check_hostname = False
            lax.verify_mode = ssl.CERT_NONE
            raw = _open(lax)
    except urllib.error.HTTPError as e:
        raise MirrorError({401: "Schlüssel abgelehnt – auf dem Original neu erstellt oder gelöscht?",
                           404: "Unter der Adresse läuft kein (aktuelles) EarnWatch.",
                           429: "Zu viele Versuche – kurz warten."}.get(e.code, f"Fehler {e.code} vom Original.")) from None
    except (urllib.error.URLError, OSError, ValueError):
        raise MirrorError("Original nicht erreichbar – läuft es, und stimmt die Adresse?") from None
    if len(raw) > MAX_RESP or len(raw) < 30:
        raise MirrorError("Unerwartete Antwort vom Original.")
    try:
        plain = AESGCM(enc).decrypt(raw[:12], raw[12:], nonce.encode())
    except (InvalidTag, ValueError):
        raise MirrorError("Antwort nicht echt (falscher Schlüssel oder verändert) – verworfen.") from None
    d = zlib.decompressobj()
    data = d.decompress(plain, MAX_PLAIN)
    if d.unconsumed_tail:
        raise MirrorError("Antwort zu groß.")
    out = json.loads(data)
    if not isinstance(out, dict) or abs(int(out.get("t", 0)) - time.time()) > SKEW + 60:
        raise MirrorError("Antwort ist veraltet – verworfen.")
    return out


def _apply(d: dict, since: int) -> int:
    snaps = []
    last = since
    for s in d.get("snapshots") or []:
        try:
            sid, pf, ts, bal, cur = int(s[0]), str(s[1]), int(s[2]), float(s[3]), str(s[4])[:8]
        except (ValueError, TypeError, IndexError):
            continue
        if sid > last:
            last = sid
        if PF_RE.match(pf) and 0 < ts < 4_102_444_800 and abs(bal) < 1e12:
            snaps.append((pf, ts, bal, cur))
    max_id = int(d.get("max_id") or 0)
    daily, runs = [], []
    for s in d.get("imported_daily") or []:
        try:
            if PF_RE.match(str(s[0])) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(s[1])):
                daily.append((str(s[0]), str(s[1]), float(s[2])))
        except (ValueError, TypeError, IndexError):
            continue
    for s in d.get("runs") or []:
        try:
            if PF_RE.match(str(s[0])):
                runs.append((str(s[0]), int(s[1]), 1 if s[2] else 0, str(s[3] or "")[:300]))
        except (ValueError, TypeError, IndexError):
            continue
    settings = {str(k): str(v)[:200000] for k, v in (d.get("settings") or {}).items() if SETTING_RE.match(str(k))}
    configured = {str(k): int(v) for k, v in (d.get("configured") or {}).items() if PF_RE.match(str(k))}
    with db.conn() as c:
        if since and max_id < since:      # Original wurde zurückgesetzt → alles neu laden
            c.execute("DELETE FROM snapshots")
        c.executemany("INSERT INTO snapshots(platform, ts, balance, currency) VALUES (?,?,?,?)", snaps)
        c.execute("DELETE FROM imported_daily")
        c.executemany("INSERT INTO imported_daily(platform, day, amount) VALUES (?,?,?)", daily)
        c.execute("DELETE FROM runs")
        c.executemany("INSERT INTO runs(platform, ts, ok, message) VALUES (?,?,?,?)", runs)
        c.executemany("INSERT OR REPLACE INTO settings(key, value) VALUES (?,?)", list(settings.items()))
        c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('mirror_configured', ?)", (json.dumps(configured),))
    return 0 if (since and max_id < since) else last


_sync_lock = threading.Lock()


def sync() -> str | None:
    """Holt neue Daten vom Original. Gibt None (ok) oder eine Fehlermeldung zurück."""
    cfg = mirror_config()
    if not cfg:
        return "Kein Spiegel eingerichtet."
    with _sync_lock:
        try:
            master = _unb64u(cfg["k"])
            since = int(db.get_setting("mirror_last_id", "0") or 0)
            for _ in range(50):             # große Verläufe in Stücken holen
                d = _fetch(cfg["u"], master, since)
                new = _apply(d, since)
                if new == 0 and since:      # Original zurückgesetzt: neu ab 0
                    since = 0
                    continue
                done = new >= int(d.get("max_id") or 0) or new == since
                since = new
                db.set_setting("mirror_last_id", str(since))
                if done:
                    break
        except MirrorError as e:
            db.set_setting("mirror_error", str(e))
            return str(e)
        except (ValueError, TypeError, KeyError):
            db.set_setting("mirror_error", "Unerwartete Antwort vom Original.")
            return "Unerwartete Antwort vom Original."
    db.set_setting("mirror_error", "")
    db.set_setting("mirror_last_ok", str(int(time.time())))
    return None
