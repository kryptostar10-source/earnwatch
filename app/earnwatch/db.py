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
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
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


# --- Benutzer -------------------------------------------------------------
def user_count() -> int:
    with conn() as c:
        return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]


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


def load_credentials(platform: str) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT blob FROM credentials WHERE platform=?", (platform,)).fetchone()
    return crypto.decrypt(row["blob"]) if row else None


def configured_platforms() -> dict[str, int]:
    with conn() as c:
        return {r["platform"]: r["updated"] for r in c.execute("SELECT platform, updated FROM credentials")}


def delete_credentials(platform: str) -> None:
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
