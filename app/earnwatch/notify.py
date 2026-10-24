"""Benachrichtigungen aufs Handy – über ntfy (App „ntfy“) und/oder Telegram.

Sicherheit:
- Zugangsdaten (ntfy-Token, Telegram-Bot-Token) liegen verschlüsselt in der Datenbank.
- Nur HTTPS, Weiterleitungen nur auf denselben Server (siehe collectors._opener).
- Nachrichten enthalten nie Passwörter oder Tokens.
- Senden passiert im Hintergrund; ein Fehler beim Senden stört EarnWatch nie.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
import urllib.parse

from . import db
from .collectors import _opener, _request, CollectError

log = logging.getLogger("earnwatch.notify")

EVENTS = {
    "payout": "Auszahlung möglich",
    "error": "Abruf fehlgeschlagen / Login abgelaufen",
    "miner": "Geräte / Duino-Miner offline",
    "login": "Neue Anmeldung bei EarnWatch",
    "security": "Sicherheits-Änderungen (Passwort, 2FA)",
    "summary": "Tageszusammenfassung",
    "achievement": "Erfolg freigeschaltet",
    "container": "Container gestoppt / abgestürzt",
    "expiry": "Login läuft bald ab",
}
DEFAULT_EVENTS = ["payout", "error", "miner", "login", "security", "summary", "achievement", "container", "expiry"]
NEW_EVENTS = ["achievement", "container", "expiry"]   # bei bestehenden Einstellungen einmalig mit einschalten
TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{12,64}$")
_lock = threading.Lock()
_recent: dict[str, float] = {}


def random_topic() -> str:
    return "earnwatch-" + secrets.token_urlsafe(18).replace("_", "x").replace("-", "y")


def config() -> dict:
    cfg = db.load_credentials("_notify") or {}
    cfg.setdefault("ntfy_server", "https://ntfy.sh")
    try:
        cfg["events"] = json.loads(db.get_setting("notify_events", json.dumps(DEFAULT_EVENTS)))
    except ValueError:
        cfg["events"] = DEFAULT_EVENTS
    if db.get_setting("notify_events_v2", "") != "1":
        # Neue Meldungsarten bei einem Update einmal automatisch einschalten (danach frei abwählbar)
        cfg["events"] = list(dict.fromkeys(cfg["events"] + NEW_EVENTS))
        db.set_setting("notify_events", json.dumps(cfg["events"]))
        db.set_setting("notify_events_v2", "1")
    cfg["summary_hour"] = int(db.get_setting("notify_summary_hour", "21"))
    return cfg


def enabled(cfg: dict | None = None) -> bool:
    cfg = cfg or config()
    return bool(cfg.get("ntfy_topic") or (cfg.get("tg_token") and cfg.get("tg_chat")))


def validate(form: dict) -> tuple[dict | None, str | None]:
    """Prüft die Eingaben aus den Einstellungen. Gibt (neue_konfig, fehler) zurück."""
    old = db.load_credentials("_notify") or {}
    server = (form.get("ntfy_server") or "https://ntfy.sh").strip().rstrip("/")
    topic = (form.get("ntfy_topic") or "").strip()
    token = (form.get("ntfy_token") or "").strip() or old.get("ntfy_token", "")
    tg_token = (form.get("tg_token") or "").strip() or old.get("tg_token", "")
    tg_chat = (form.get("tg_chat") or "").strip()
    if form.get("ntfy_token_clear") == "1":
        token = ""
    if form.get("tg_clear") == "1":
        tg_token, tg_chat = "", ""
    p = urllib.parse.urlparse(server)
    if p.scheme != "https" or not p.hostname or p.path not in ("", "/") or p.query or p.username:
        return None, "ntfy-Server muss eine https://-Adresse ohne Pfad sein, z. B. https://ntfy.sh"
    if topic and not TOPIC_RE.match(topic):
        return None, "ntfy-Thema: 12–64 Zeichen, nur Buchstaben, Ziffern, - und _ (wie ein Passwort wählen!)"
    if tg_token and not re.match(r"^\d{5,15}:[A-Za-z0-9_-]{30,64}$", tg_token):
        return None, "Telegram-Bot-Token sieht nicht richtig aus (Format 123456:ABC…)"
    if tg_chat and not re.match(r"^-?\d{3,20}$", tg_chat):
        return None, "Telegram-Chat-ID muss eine Zahl sein"
    return {"ntfy_server": server, "ntfy_topic": topic, "ntfy_token": token,
            "tg_token": tg_token, "tg_chat": tg_chat}, None


def _send_now(cfg: dict, title: str, message: str, priority: int, tags: list[str]) -> list[str]:
    errors = []
    op = _opener()
    if cfg.get("ntfy_topic"):
        hdr = {"Authorization": f"Bearer {cfg['ntfy_token']}"} if cfg.get("ntfy_token") else {}
        try:
            st, _, _ = _request(op, cfg["ntfy_server"] + "/", method="POST", headers=hdr, body={
                "topic": cfg["ntfy_topic"], "title": title[:120], "message": message[:3000],
                "priority": max(1, min(5, priority)), "tags": tags[:5]})
            if st not in (200, 201):
                errors.append(f"ntfy antwortet mit HTTP {st}")
        except CollectError as e:
            errors.append(f"ntfy: {e}")
    if cfg.get("tg_token") and cfg.get("tg_chat"):
        try:
            st, _, _ = _request(op, f"https://api.telegram.org/bot{cfg['tg_token']}/sendMessage", method="POST",
                                body={"chat_id": cfg["tg_chat"], "text": f"{title}\n{message}"[:4000],
                                      "disable_web_page_preview": True})
            if st != 200:
                errors.append(f"Telegram antwortet mit HTTP {st}")
        except CollectError as e:
            errors.append(f"Telegram: {e}")
    for e in errors:
        log.warning("Benachrichtigung fehlgeschlagen: %s", e)
    return errors


def send(event: str, title: str, message: str, priority: int = 3, tags: list[str] | None = None,
         dedupe: str | None = None, dedupe_seconds: int = 900, wait: bool = False):
    """Sendet eine Nachricht, wenn der Typ eingeschaltet ist. Standard: im Hintergrund."""
    cfg = config()
    if not enabled(cfg) or (event != "test" and event not in cfg["events"]):
        return []
    if dedupe:
        with _lock:
            now = time.time()
            if now - _recent.get(dedupe, 0) < dedupe_seconds:
                return []
            _recent[dedupe] = now
    if wait:
        return _send_now(cfg, title, message, priority, tags or [])
    threading.Thread(target=_send_now, args=(cfg, title, message, priority, tags or []), daemon=True).start()
    return []


def short_agent(ua: str) -> str:
    ua = ua or ""
    dev = ("iPhone" if "iPhone" in ua else "iPad" if "iPad" in ua else "Android" if "Android" in ua else
           "Mac" if "Macintosh" in ua else "Windows" if "Windows" in ua else "Linux" if "Linux" in ua else "Gerät")
    br = ("Edge" if "Edg/" in ua else "Chrome" if "Chrome/" in ua else "Firefox" if "Firefox/" in ua else
          "Safari" if "Safari/" in ua else "Browser")
    return f"{br} auf {dev}"
