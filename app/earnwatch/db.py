"""SQLite-Speicher: Benutzer, verschlüsselte Zugangsdaten, Kontostände."""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager

from . import config, crypto

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    pw_hash TEXT NOT NULL,
    session_ver INTEGER NOT NULL DEFAULT 1,
    created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS credentials (
    platform TEXT PRIMARY KEY,
    blob BLOB NOT NULL,
    updated INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,
    ts INTEGER NOT NULL,
    balance REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD'
);
CREATE INDEX IF NOT EXISTS ix_snap ON snapshots(platform, ts);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,
    ts INTEGER NOT NULL,
    ok INTEGER NOT NULL,
    message TEXT
);
CREATE INDEX IF NOT EXISTS ix_runs ON runs(platform, ts);
CREATE TABLE IF NOT EXISTS imported_daily (
    platform TEXT NOT NULL,
    day TEXT NOT NULL,
    amount REAL NOT NULL,
    PRIMARY KEY (platform, day)
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS passkeys (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    cred_id TEXT UNIQUE NOT NULL,
    public_key TEXT NOT NULL,
    alg INTEGER NOT NULL,
    sign_count INTEGER NOT NULL DEFAULT 0,
    rp_id TEXT NOT NULL,
    name TEXT NOT NULL,
    created INTEGER NOT NULL,
    last_used INTEGER
);
"""


@contextmanager
def conn():
    with _lock:
        c = sqlite3.connect(config.DB_PATH, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()


def init() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with conn() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        cols = {r[1] for r in c.execute("PRAGMA table_info(users)")}
        for col, typ in (("totp_secret", "BLOB"), ("totp_pending", "BLOB"),
                         ("totp_last", "INTEGER NOT NULL DEFAULT 0"), ("recovery", "TEXT")):
            if col not in cols:
                c.execute(f"ALTER TABLE users ADD COLUMN {col} {typ}")  # feste Namen, keine Eingaben


# --- Benutzer -------------------------------------------------------------
def user_count() -> int:
    with conn() as c:
        return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def create_first_user(username: str, pw_hash: str) -> bool:
    """Legt das Admin-Konto nur an, wenn es noch KEIN Konto gibt (atomar, gegen Wettläufe)."""
    with conn() as c:
        cur = c.execute(
            "INSERT INTO users(username, pw_hash, created) "
            "SELECT ?,?,? WHERE NOT EXISTS (SELECT 1 FROM users)",
            (username, pw_hash, int(time.time())),
        )
        return cur.rowcount == 1


def create_user(username: str, pw_hash: str) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO users(username, pw_hash, created) VALUES (?,?,?)",
            (username, pw_hash, int(time.time())),
        )


def get_user_by_name(username: str):
    with conn() as c:
        return c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()


def get_user(uid: int):
    with conn() as c:
        return c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def set_password(uid: int, pw_hash: str) -> None:
    with conn() as c:
        c.execute(
            "UPDATE users SET pw_hash=?, session_ver=session_ver+1 WHERE id=?", (pw_hash, uid)
        )


# --- Zwei-Faktor ----------------------------------------------------------
def totp_secret(user) -> str | None:
    if not user or not user["totp_secret"]:
        return None
    d = crypto.decrypt(user["totp_secret"])
    return d.get("s") if d else None


def set_totp_pending(uid: int, secret: str | None) -> None:
    with conn() as c:
        c.execute("UPDATE users SET totp_pending=? WHERE id=?",
                  (crypto.encrypt({"s": secret}) if secret else None, uid))


def totp_pending(user) -> str | None:
    if not user or not user["totp_pending"]:
        return None
    d = crypto.decrypt(user["totp_pending"])
    return d.get("s") if d else None


def enable_totp(uid: int, secret: str, counter: int, recovery_hashes: list[str]) -> None:
    import json
    with conn() as c:
        c.execute("UPDATE users SET totp_secret=?, totp_pending=NULL, totp_last=?, recovery=?, "
                  "session_ver=session_ver+1 WHERE id=?",
                  (crypto.encrypt({"s": secret}), counter, json.dumps(recovery_hashes), uid))


def disable_totp(uid: int) -> None:
    with conn() as c:
        c.execute("UPDATE users SET totp_secret=NULL, totp_pending=NULL, totp_last=0, recovery=NULL, "
                  "session_ver=session_ver+1 WHERE id=?", (uid,))


def set_totp_last(uid: int, counter: int) -> bool:
    """Speichert den zuletzt benutzten Zeitschritt – atomar, nur wenn er neuer ist."""
    with conn() as c:
        cur = c.execute("UPDATE users SET totp_last=? WHERE id=? AND totp_last<?", (counter, uid, counter))
        return cur.rowcount == 1


def recovery_hashes(user) -> list[str]:
    import json
    try:
        return json.loads(user["recovery"] or "[]")
    except ValueError:
        return []


def set_recovery(uid: int, hashes: list[str], expect_old: str | None = None) -> bool:
    import json
    with conn() as c:
        if expect_old is None:
            cur = c.execute("UPDATE users SET recovery=? WHERE id=?", (json.dumps(hashes), uid))
        else:  # nur ersetzen, wenn sich die Liste nicht zwischendurch geändert hat
            cur = c.execute("UPDATE users SET recovery=? WHERE id=? AND recovery=?", (json.dumps(hashes), uid, expect_old))
        return cur.rowcount == 1


def bump_session(uid: int) -> None:
    with conn() as c:
        c.execute("UPDATE users SET session_ver=session_ver+1 WHERE id=?", (uid,))


# --- Zugangsdaten (immer verschlüsselt) -----------------------------------
def save_credentials(platform: str, data: dict) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO credentials(platform, blob, updated) VALUES (?,?,?) "
            "ON CONFLICT(platform) DO UPDATE SET blob=excluded.blob, updated=excluded.updated",
            (platform, crypto.encrypt(data), int(time.time())),
        )
    remember_expiry(platform, data)


def token_expiry(data: dict) -> int | None:
    """Liest das Ablaufdatum aus Tokens im JWT-Format (z. B. Grass). Nur lesen, nichts prüfen/senden."""
    import base64
    import json as _json
    best = None
    for v in (data or {}).values():
        v = str(v or "").strip().strip('"')
        if v.lower().startswith("bearer "):
            v = v[7:]
        parts = v.split(".")
        if len(parts) != 3 or len(v) > 8000:
            continue
        try:
            payload = _json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
            exp = int(payload.get("exp")) if isinstance(payload, dict) and payload.get("exp") else None
        except (ValueError, TypeError):
            continue
        if exp and 1_500_000_000 < exp < 4_000_000_000:
            best = exp if best is None else min(best, exp)
    return best


def remember_expiry(platform: str, data: dict) -> None:
    exp = token_expiry(data)
    set_setting(f"cred_exp_{platform}", str(exp or ""))


def load_credentials(platform: str) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT blob FROM credentials WHERE platform=?", (platform,)).fetchone()
    return crypto.decrypt(row["blob"]) if row else None


def configured_platforms() -> dict[str, int]:
    with conn() as c:
        return {r["platform"]: r["updated"] for r in c.execute("SELECT platform, updated FROM credentials")}


def delete_credentials(platform: str) -> None:
    set_setting(f"cred_exp_{platform}", "")
    with conn() as c:
        c.execute("DELETE FROM credentials WHERE platform=?", (platform,))


# --- Messwerte ------------------------------------------------------------
def add_snapshot(platform: str, balance: float, currency: str = "USD", ts: int | None = None) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO snapshots(platform, ts, balance, currency) VALUES (?,?,?,?)",
            (platform, ts or int(time.time()), balance, currency),
        )


def snapshots(since: int = 0) -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute(
            "SELECT platform, ts, balance, currency FROM snapshots WHERE ts>=? ORDER BY platform, ts",
            (since,),
        ).fetchall()


def save_history(platform: str, days: dict) -> None:
    with conn() as c:
        c.executemany(
            "INSERT INTO imported_daily(platform, day, amount) VALUES (?,?,?) "
            "ON CONFLICT(platform, day) DO UPDATE SET amount=excluded.amount",
            [(platform, d, float(a)) for d, a in days.items()],
        )


def imported_history() -> dict:
    with conn() as c:
        rows = c.execute("SELECT platform, day, amount FROM imported_daily").fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(r["platform"], {})[r["day"]] = r["amount"]
    return out


def add_run(platform: str, ok: bool, message: str | None) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO runs(platform, ts, ok, message) VALUES (?,?,?,?)",
            (platform, int(time.time()), 1 if ok else 0, (message or "")[:300]),
        )
        # nur die letzten 500 Läufe je Plattform behalten
        c.execute(
            "DELETE FROM runs WHERE platform=? AND id NOT IN "
            "(SELECT id FROM runs WHERE platform=? ORDER BY ts DESC LIMIT 500)",
            (platform, platform),
        )


def last_runs() -> dict[str, sqlite3.Row]:
    with conn() as c:
        rows = c.execute(
            "SELECT r.* FROM runs r JOIN (SELECT platform, MAX(id) AS mid FROM runs GROUP BY platform) m "
            "ON r.id = m.mid"
        ).fetchall()
    return {r["platform"]: r for r in rows}


# --- Einstellungen --------------------------------------------------------
def get_setting(key: str, default: str) -> str:
    with conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO settings(key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


# --- Passkeys (Face ID / Touch ID) ----------------------------------------
def add_passkey(uid: int, cred_id: str, public_key: str, alg: int, sign_count: int, rp_id: str, name: str) -> None:
    with conn() as c:
        c.execute("INSERT INTO passkeys (user_id, cred_id, public_key, alg, sign_count, rp_id, name, created) "
                  "VALUES (?,?,?,?,?,?,?,?)", (uid, cred_id, public_key, alg, sign_count, rp_id, name[:60], int(time.time())))


def passkeys(uid: int) -> list:
    with conn() as c:
        return c.execute("SELECT * FROM passkeys WHERE user_id=? ORDER BY created", (uid,)).fetchall()


def passkey_by_cred(cred_id: str):
    with conn() as c:
        return c.execute("SELECT * FROM passkeys WHERE cred_id=?", (cred_id,)).fetchone()


def passkey_used(pk_id: int, old_count: int, new_count: int) -> bool:
    """Zähler atomar hochsetzen – schlägt fehl, wenn parallel schon benutzt (Schutz gegen Klone/Replay)."""
    with conn() as c:
        cur = c.execute("UPDATE passkeys SET sign_count=?, last_used=? WHERE id=? AND sign_count=?",
                        (new_count, int(time.time()), pk_id, old_count))
        return cur.rowcount == 1


def delete_passkey(uid: int, pk_id: int) -> bool:
    with conn() as c:
        return c.execute("DELETE FROM passkeys WHERE id=? AND user_id=?", (pk_id, uid)).rowcount == 1
