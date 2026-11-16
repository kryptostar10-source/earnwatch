"""App-Symbole der Dienste für das Dock (Stil „Desktop“).

Der Server holt das offizielle Symbol einmal direkt von der Webseite des Dienstes
(apple-touch-icon bzw. favicon – so wie ein Browser fürs Lesezeichen) und speichert es lokal.
Danach lädt der Browser nur noch von EarnWatch selbst (CSP img-src 'self' bleibt).

Sicherheit:
* nur HTTPS, nur zur Domain des Dienstes (auch bei Weiterleitungen), feste Zeitgrenzen
* nur echte Rasterbilder (PNG/ICO/JPEG/GIF/WebP, an den ersten Bytes erkannt) – kein SVG/HTML
* höchstens 300 KB, Dateiname nur aus der festen Plattform-ID
"""
from __future__ import annotations

import logging
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from . import config, db
from .collectors import PLATFORMS, UA

log = logging.getLogger("earnwatch")
ICON_DIR = config.DATA_DIR / "icons"
MAX_BYTES = 300_000
REFRESH = 30 * 86400      # Symbol alle 30 Tage erneuern
RETRY = 86400             # nach Fehlschlag frühestens morgen wieder
PF_RE = re.compile(r"^[a-z0-9_]{1,32}$")
TYPES = {"png": "image/png", "ico": "image/x-icon", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}


def _kind(data: bytes) -> str | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"\x00\x00\x01\x00":
        return "ico"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _root(host: str) -> str:
    host = host.split(":")[0]
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) > 2 else host


def _hosts(pf: str) -> list[str]:
    url = PLATFORMS.get(pf, {}).get("dashboard") or ""
    u = urllib.parse.urlparse(url)
    if u.scheme != "https" or not u.hostname:
        return []
    host = u.netloc.lower()
    out = [host]
    if _root(host) != host:
        out.append(_root(host))
    return out


class _Redirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 4

    def __init__(self, root: str):
        super().__init__()
        self.root = root

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        u = urllib.parse.urlparse(newurl)
        h = (u.hostname or "").lower()
        if u.scheme != "https" or not (h == self.root or h.endswith("." + self.root)):
            raise urllib.error.HTTPError(req.full_url, code, "fremde Weiterleitung", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _NoHTTP(urllib.request.BaseHandler):
    def http_open(self, req):
        raise urllib.error.URLError("nur HTTPS")


def _get(url: str, root: str, limit: int) -> tuple[bytes, str] | None:
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl.create_default_context()),
                                         _Redirect(root), _NoHTTP())
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "image/*,text/html;q=0.8,*/*;q=0.5"})
    try:
        with opener.open(req, timeout=12) as r:
            if r.status != 200:
                return None
            data = r.read(limit + 1)
            return (data, r.geturl()) if len(data) <= limit else None
    except (urllib.error.URLError, OSError, ValueError, ssl.SSLError):
        return None


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.found: list[tuple[int, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag != "link":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        rel = a.get("rel", "").lower().split()
        href = a.get("href", "").strip()
        if not href or href.lower().endswith(".svg") or "svg" in a.get("type", ""):
            return
        if "apple-touch-icon" in rel or "apple-touch-icon-precomposed" in rel:
            score = 1000
        elif "icon" in rel:
            m = re.match(r"(\d+)x", a.get("sizes", ""))
            score = int(m.group(1)) if m else 16
        else:
            return
        self.found.append((score, href))


def fetch(pf: str) -> str | None:
    """Holt das Symbol einer Plattform. Gibt die Endung zurück oder None."""
    if not PF_RE.match(pf):
        return None
    for host in _hosts(pf):
        root = _root(host)
        candidates = [f"https://{host}/apple-touch-icon.png"]
        page = _get(f"https://{host}/", root, 1_500_000)
        if page:
            p = _Links()
            try:
                p.feed(page[0].decode("utf-8", "replace"))
            except Exception:   # kaputtes HTML ist kein Grund abzubrechen
                pass
            for _, href in sorted(p.found, reverse=True)[:4]:
                candidates.append(urllib.parse.urljoin(page[1], href))
        candidates.append(f"https://{host}/favicon.ico")
        for url in candidates:
            u = urllib.parse.urlparse(url)
            h = (u.hostname or "").lower()
            if u.scheme != "https" or not (h == root or h.endswith("." + root)):
                continue   # Symbole nur von der Domain des Dienstes selbst
            got = _get(url, root, MAX_BYTES)
            if not got or len(got[0]) < 100:
                continue
            kind = _kind(got[0])
            if not kind:
                continue
            ICON_DIR.mkdir(parents=True, exist_ok=True)
            for old in ICON_DIR.glob(f"{pf}.*"):
                old.unlink(missing_ok=True)
            tmp = ICON_DIR / f".{pf}.tmp"
            tmp.write_bytes(got[0])
            tmp.replace(ICON_DIR / f"{pf}.{kind}")
            return kind
    return None


def refresh(force: bool = False) -> int:
    """Holt fehlende/alte Symbole der eingerichteten Plattformen. Läuft im Hintergrund."""
    n = 0
    now = int(time.time())
    for pf in db.configured_platforms():
        if pf not in PLATFORMS or not PF_RE.match(pf):
            continue
        state = db.get_setting(f"icon_{pf}", "")
        kind, ts, tried = (state.split("|") + ["", "0", "0"])[:3]
        ts, tried = int(ts or 0), int(tried or 0)
        have = kind in TYPES and (ICON_DIR / f"{pf}.{kind}").exists()
        if not force and ((have and now - ts < REFRESH) or (not have and now - tried < RETRY)):
            continue
        got = fetch(pf)
        if got:
            db.set_setting(f"icon_{pf}", f"{got}|{now}|{now}")
            n += 1
        else:
            db.set_setting(f"icon_{pf}", f"{kind if have else ''}|{ts}|{now}")
            log.info("Kein Symbol für %s gefunden", pf)
    return n


def available(pf: str) -> tuple[str, int] | None:
    """(Endung, Zeitstempel) wenn ein Symbol gespeichert ist."""
    if not PF_RE.match(pf):
        return None
    kind, ts = (db.get_setting(f"icon_{pf}", "").split("|") + ["", "0"])[:2]
    if kind in TYPES and (ICON_DIR / f"{pf}.{kind}").exists():
        return kind, int(ts or 0)
    return None
