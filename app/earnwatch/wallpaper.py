"""Foto als Hintergrund (Stile Folio & Desktop): Auswahl, Link aus dem Internet oder Upload.

Sicherheit beim Herunterladen eines Links (Schutz vor „SSRF“ – Abtasten des Heimnetzes über den Server):
* nur HTTPS, Standard-Port, keine Zugangsdaten in der Adresse
* jede Adresse (auch jede Weiterleitung) muss auf ÖFFENTLICHE IP-Adressen zeigen –
  192.168.x, 10.x, localhost, Router, Proxmox usw. werden abgelehnt
* höchstens 15 MB, nur echte JPEG/PNG/WebP-Bilder (an den ersten Bytes erkannt)
Gespeichert wird nur eine Datei „current.<endung>“ im Datenordner; ausgeliefert nur angemeldet,
mit nosniff + Sandbox-CSP.
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config, db

DIR = config.DATA_DIR / "wallpaper"
MAX_BYTES = 15 * 1024 * 1024
TYPES = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
UA = "EarnWatch/1.0 (self-hosted dashboard; wallpaper download)"

# Gemeinfreie Fotos (Wikimedia Commons). Special:FilePath liefert eine verkleinerte Fassung (2560 px).
_FP = "https://commons.wikimedia.org/wiki/Special:FilePath/{}?width=2560"
PRESETS = {
    "earthrise": {"name": "Erdaufgang", "file": "NASA-Apollo8-Dec24-Earthrise.jpg",
                  "credit": "NASA / Bill Anders, Apollo 8 – gemeinfrei"},
    "aurora": {"name": "Polarlicht (ISS)", "file": "Aurora_Australis_From_ISS_edit1.JPG",
               "credit": "NASA, ISS Expedition 23 – gemeinfrei"},
    "yosemite": {"name": "Yosemite", "file": "Yosemite_by_Carol_M._Highsmith.jpg",
                 "credit": "Carol M. Highsmith, Library of Congress – gemeinfrei"},
}


class WallError(Exception):
    pass


def kind(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _check_url(url: str) -> urllib.parse.ParseResult:
    u = urllib.parse.urlparse(url.strip())
    if u.scheme != "https" or not u.hostname or u.username or u.password or (u.port not in (None, 443)):
        raise WallError("Bitte einen https://-Link zu einem Bild angeben.")
    try:
        infos = socket.getaddrinfo(u.hostname, 443, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        raise WallError("Die Adresse wurde nicht gefunden.") from None
    for info in infos:
        if not _public(info[4][0]):
            raise WallError("Links ins eigene Netz (Router, Server, localhost …) sind aus Sicherheitsgründen gesperrt.")
    return u


_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")
_SIX_TO_FOUR = ipaddress.ip_network("2002::/16")


def _public(addr: str) -> bool:
    """Nur echte öffentliche Adressen – auch IPv4, die in IPv6 versteckt ist (NAT64, 6to4, Teredo)."""
    ip = ipaddress.ip_address(addr.split("%")[0])
    if not ip.is_global or ip.is_multicast:
        return False
    if ip.version == 6:
        inner = None
        if ip in _NAT64 or ip in _NAT64_LOCAL:
            inner = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        elif ip in _SIX_TO_FOUR:
            inner = ip.sixtofour
        elif ip.teredo:
            inner = ip.teredo[1]
        if inner is not None and (not inner.is_global or inner.is_multicast):
            return False
    return True


class _PinnedHTTPS(http.client.HTTPSConnection):
    """Verbindet nur zu einer vorher geprüften öffentlichen IP – gegen DNS-Rebinding
    (Name zeigt bei der Prüfung nach draußen, beim Verbinden plötzlich ins Heimnetz)."""

    def connect(self):
        if self._tunnel_host:   # über einen Proxy: der Proxy entscheidet, Standardweg
            return super().connect()
        infos = socket.getaddrinfo(self.host, self.port or 443, proto=socket.IPPROTO_TCP)
        if not infos or not all(_public(i[4][0]) for i in infos):
            raise OSError("Ziel ist keine öffentliche Adresse")
        family, _, _, _, sa = infos[0]
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(sa)
            if not _public(sock.getpeername()[0]):   # was wirklich verbunden wurde
                raise OSError("Ziel ist keine öffentliche Adresse")
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


class _PinnedHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PinnedHTTPS, req, context=self._context)


class _Redirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)   # auch jede Weiterleitung muss öffentlich + https sein
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _NoHTTP(urllib.request.BaseHandler):
    def http_open(self, req):
        raise urllib.error.URLError("nur HTTPS")


def download(url: str) -> tuple[bytes, str]:
    if len(url) > 2000:
        raise WallError("Der Link ist zu lang.")
    _check_url(url)
    opener = urllib.request.build_opener(_PinnedHandler(context=ssl.create_default_context()),
                                         _Redirect(), _NoHTTP())
    req = urllib.request.Request(url.strip(), headers={"User-Agent": UA, "Accept": "image/jpeg,image/png,image/webp,image/*;q=0.8"})
    try:
        with opener.open(req, timeout=25) as r:
            data = r.read(MAX_BYTES + 1)
    except WallError:
        raise
    except urllib.error.HTTPError as e:
        raise WallError(f"Die Seite hat das Bild verweigert (Fehler {e.code}).") from None
    except (urllib.error.URLError, OSError, ValueError, ssl.SSLError):
        raise WallError("Das Bild konnte nicht geladen werden.") from None
    if len(data) > MAX_BYTES:
        raise WallError("Das Bild ist größer als 15 MB.")
    k = kind(data)
    if not k:
        raise WallError("Unter dem Link liegt kein JPEG-, PNG- oder WebP-Bild. Tipp: Rechtsklick aufs Bild → „Bildadresse kopieren“.")
    return data, k


def store(data: bytes, k: str, source: str) -> None:
    if k not in TYPES or len(data) > MAX_BYTES:
        raise WallError("Ungültiges Bild.")
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = DIR / ".current.tmp"
    tmp.write_bytes(data)
    for old in DIR.glob("current.*"):
        old.unlink(missing_ok=True)
    tmp.replace(DIR / f"current.{k}")
    db.set_setting("wallpaper_photo", f"{k}|{int(time.time())}|{source[:300]}")
    db.set_setting("wallpaper", "photo")


def current() -> tuple[str, int, str] | None:
    k, ts, src = (db.get_setting("wallpaper_photo", "").split("|", 2) + ["", "0", ""])[:3]
    if k in TYPES and (DIR / f"current.{k}").exists():
        return k, int(ts or 0), src
    return None


def preset_url(pid: str) -> str:
    return _FP.format(urllib.parse.quote(PRESETS[pid]["file"]))
