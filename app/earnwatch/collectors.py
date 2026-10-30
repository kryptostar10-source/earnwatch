"""Abruf der Kontostände bei Honeygain, EarnApp, Grass, Bytelixir und Duino-Coin.

Wichtig für die Sicherheit:
- Es werden nur feste HTTPS-Adressen der Anbieter angesprochen (kein SSRF möglich).
- Fehlermeldungen werden selbst formuliert, nie der rohe Exception-Text –
  so landen Passwörter/Tokens niemals im Log oder in der Oberfläche.
"""
from __future__ import annotations

import http.cookiejar
import json
import logging
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import time
from dataclasses import dataclass, field

log = logging.getLogger("earnwatch.collect")

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
TIMEOUT = 30
_SSL = ssl.create_default_context()  # Zertifikate werden immer geprüft


@dataclass
class Result:
    platform: str
    balance: float | None
    message: str | None = None          # Fehlertext (ohne Geheimnisse)
    price_usd: float | None = None      # Kurs der Einheit in USD (z. B. DUCO)
    info: dict = field(default_factory=dict)   # Zusatzinfos (Miner, Hashrate …)
    new_creds: dict | None = None       # aufgefrischte Cookies, werden verschlüsselt gespeichert
    history: dict | None = None         # Tageseinnahmen vom Anbieter {"YYYY-MM-DD": betrag}

    @property
    def ok(self) -> bool:
        return self.balance is not None


class CollectError(Exception):
    pass


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Folgt Weiterleitungen nur per HTTPS und nur auf denselben Server.

    Standardmäßig würde Python den Authorization-Header (Login-Token) bei einer
    Weiterleitung an JEDE Adresse mitschicken – auch über unverschlüsseltes HTTP.
    """
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urllib.parse.urlparse(req.full_url), urllib.parse.urlparse(newurl)
        if new.scheme != "https" or new.hostname != old.hostname:
            raise urllib.error.HTTPError(req.full_url, code, "Weiterleitung auf fremden Server blockiert", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _NoPlainHTTP(urllib.request.BaseHandler):
    def http_open(self, req):  # niemals unverschlüsselt
        raise urllib.error.URLError("HTTP ohne TLS ist gesperrt")


def _opener(jar: http.cookiejar.CookieJar | None = None):
    handlers = [urllib.request.HTTPSHandler(context=_SSL), _SafeRedirect(), _NoPlainHTTP()]
    if jar is not None:
        handlers.append(urllib.request.HTTPCookieProcessor(jar))
    return urllib.request.build_opener(*handlers)


def _request(opener, url: str, *, method="GET", body: dict | None = None, headers=None, accept="application/json"):
    """Gibt (status, body, finale_url) zurück."""
    hdrs = {"User-Agent": UA, "Accept": accept}
    hdrs.update(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read(3_000_000), resp.geturl()
    except urllib.error.HTTPError as e:
        return e.code, b"", url
    except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as e:
        raise CollectError(f"Netzwerkfehler ({type(e).__name__})") from None


def _json(raw: bytes) -> dict:
    try:
        return json.loads(raw)
    except ValueError:
        raise CollectError("Antwort ist kein JSON – hat der Anbieter seine API geändert?") from None


def _cookie(name: str, value: str, domain: str) -> http.cookiejar.Cookie:
    return http.cookiejar.Cookie(
        0, name, value, None, False, domain, False, False, "/", True,
        True, None, False, None, None, {},
    )


# --- Geräte zählen (für „x von y Geräten online“) -------------------------
_ONLINE_WORDS = {"active", "online", "running", "connected", "streaming", "working", "on"}


def _device_list(data) -> list:
    """Holt die Geräteliste aus verschiedenen Antwortformen ([..], {data:[..]}, {data:{items:[..]}} …)."""
    for _ in range(3):
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)][:1000]
        if not isinstance(data, dict):
            return []
        data = next((data[k] for k in ("data", "devices", "items", "results", "list") if k in data), None)
    return []


def _is_online(d: dict) -> bool | None:
    for k in ("online", "is_online", "isOnline", "active", "is_active", "isActive", "connected"):
        if isinstance(d.get(k), bool):
            return d[k]
    st = d.get("status") or d.get("state") or d.get("connection_status")
    if isinstance(st, str):
        return st.strip().lower() in _ONLINE_WORDS
    if isinstance(st, bool):
        return st
    return None


def _device_info(devices: list, online: int | None = None) -> dict:
    """Nur Zahlen speichern – keine Gerätenamen oder IP-Adressen."""
    total = len(devices)
    if online is None:
        flags = [_is_online(d) for d in devices]
        online = sum(1 for f in flags if f) if any(f is not None for f in flags) else None
    out = {"geraete": total}
    if online is not None:
        out["online"] = max(0, min(int(online), total))
    return out


def _safe_devices(fn) -> dict:
    """Geräte-Abruf ist ein Extra: Fehler dort dürfen den Kontostand nie verhindern."""
    try:
        return fn() or {}
    except Exception:  # noqa: BLE001
        return {}


# --- Honeygain ------------------------------------------------------------
HG_API = "https://dashboard.honeygain.com/api"
_hg_token_cache: dict[str, str] = {}


def _hg_session(creds: dict):
    """Meldet sich bei Honeygain an und gibt eine get(pfad)-Funktion zurück."""
    email, password = creds.get("email", ""), creds.get("password", "")
    if not email or not password:
        raise CollectError("Keine Zugangsdaten hinterlegt")
    op = _opener()
    hdr = {"Referer": "https://dashboard.honeygain.com/"}

    def login() -> str:
        st, raw, _ = _request(op, f"{HG_API}/v1/users/tokens", method="POST",
                              body={"email": email, "password": password}, headers=hdr)
        if st in (400, 401, 403, 422):
            raise CollectError("Login abgelehnt – E-Mail/Passwort prüfen (oder 2FA/Captcha aktiv)")
        if st != 200:
            raise CollectError(f"Login fehlgeschlagen (HTTP {st})")
        tok = _json(raw).get("data", {}).get("access_token")
        if not tok:
            raise CollectError("Kein Token in der Login-Antwort")
        return tok

    state = {"token": _hg_token_cache.get(email) or login()}

    def get(path: str):
        st, raw, _ = _request(op, f"{HG_API}{path}", headers={**hdr, "Authorization": f"Bearer {state['token']}"})
        if st == 401:
            state["token"] = login()
            st, raw, _ = _request(op, f"{HG_API}{path}", headers={**hdr, "Authorization": f"Bearer {state['token']}"})
        if st == 200:
            _hg_token_cache[email] = state["token"]
        return st, raw

    def mode() -> dict:
        st, raw = get("/v1/users/me")
        if st != 200:
            return {}
        return {"modus": "JumpTask" if _json(raw).get("data", {}).get("jt_toggle") else "Honeygain"}

    return get, mode


_hist_last: dict[str, float] = {}


def _hg_history(get, path: str, key: str) -> dict | None:
    """Tageseinnahmen der letzten ~30 Tage (Credits -> USD), höchstens alle 12 h abrufen."""
    import datetime as _dt
    if time.time() - _hist_last.get(key, 0) < 12 * 3600:
        return None
    st, raw = get(path)
    if st != 200:
        return None
    try:
        data = _json(raw)
    except CollectError:
        return None
    if isinstance(data.get("data"), dict):
        data = data["data"]
    out = {}
    for day, v in data.items():
        try:
            _dt.date.fromisoformat(day)
        except (TypeError, ValueError):
            continue
        if not isinstance(v, dict):
            continue
        tot = v.get("total") if isinstance(v.get("total"), dict) else None
        if tot and tot.get("credits") is not None:
            credits = float(tot.get("credits") or 0)
        else:
            credits = sum(float((v.get(k) or {}).get("credits") or 0)
                          for k in ("gathering", "cdn", "referral", "winning", "other", "bonus")
                          if isinstance(v.get(k), dict))
        if 0 <= credits < 1e7:
            out[day] = round(credits / 1000, 5)   # 1.000 Credits = 1 USD
    _hist_last[key] = time.time()
    return out or None


def collect_honeygain(creds: dict) -> Result:
    """Normales Honeygain-Guthaben (Auszahlung per PayPal/Krypto ab $20)."""
    try:
        get, mode = _hg_session(creds)
        st, raw = get("/v1/users/balances")
        if st != 200:
            raise CollectError(f"Kontostand-Abruf fehlgeschlagen (HTTP {st})")
        cents = _json(raw).get("data", {}).get("payout", {}).get("usd_cents")
        if cents is None:
            raise CollectError("Feld usd_cents fehlt – API geändert?")
        hist = _hg_history(get, "/v1/earnings/stats", "hg:" + creds.get("email", ""))

        def devices():
            st, raw = get("/v2/devices?page=1&per_page=100")
            return _device_info(_device_list(_json(raw))) if st == 200 else {}
        return Result("honeygain", round(float(cents) / 100, 4), info={**mode(), **_safe_devices(devices)},
                      history=hist)
    except CollectError as e:
        _hg_token_cache.pop(creds.get("email", ""), None)
        return Result("honeygain", None, str(e))


def collect_honeygain_jt(creds: dict) -> Result:
    """JumpTask-Einnahmen (Summe aller an JumpTask gesendeten Einnahmen, in USD)."""
    try:
        get, mode = _hg_session(creds)
        st, raw = get("/v1/earnings/jt")
        if st != 200:
            raise CollectError(f"JumpTask-Abruf fehlgeschlagen (HTTP {st})")
        d = _json(raw).get("data", {}) or {}
        cents = float(d.get("total_usd_cents") or 0) + float(d.get("bonus_usd_cents") or 0)
        credits = float(d.get("total_credits") or 0) + float(d.get("bonus_credits") or 0)
        hist = _hg_history(get, "/v1/jt-earnings/stats", "jt:" + creds.get("email", ""))
        return Result("honeygain_jt", round(cents / 100, 4), info={**mode(), "credits": round(credits, 2)},
                      history=hist)
    except CollectError as e:
        _hg_token_cache.pop(creds.get("email", ""), None)
        return Result("honeygain_jt", None, str(e))


# --- EarnApp --------------------------------------------------------------
EA_API = "https://earnapp.com/dashboard/api"
EA_VERSION = "1.627.783"


def collect_earnapp(creds: dict) -> Result:
    token = creds.get("oauth_token", "")
    if not token:
        return Result("earnapp", None, "Kein oauth-refresh-token hinterlegt")
    jar = http.cookiejar.CookieJar()
    for n, v in {"auth": "1", "auth-method": "google", "oauth-refresh-token": token}.items():
        jar.set_cookie(_cookie(n, v, "earnapp.com"))
    if creds.get("brd_sess_id"):
        jar.set_cookie(_cookie("brd_sess_id", creds["brd_sess_id"], "earnapp.com"))
    op = _opener(jar)
    q = urllib.parse.urlencode({"appid": "earnapp", "version": EA_VERSION})
    try:
        _request(op, f"{EA_API}/sec/rotate_xsrf?{q}")
        xsrf = next((c.value for c in jar if c.name == "xsrf-token"), "")
        hdr = {"X-Requested-With": "XMLHttpRequest"}
        if xsrf:
            hdr["xsrf-token"] = xsrf
        st, raw, _ = _request(op, f"{EA_API}/money?{q}", headers=hdr)
        if st in (401, 403):
            raise CollectError("Anmeldung abgelehnt – oauth-refresh-token erneuern")
        if st != 200:
            raise CollectError(f"Kontostand-Abruf fehlgeschlagen (HTTP {st})")
        data = _json(raw)
        if "error" in data:
            raise CollectError("EarnApp meldet einen Fehler – Token prüfen")
        bal = data.get("balance")
        if bal is None:
            raise CollectError("Feld balance fehlt – API geändert?")

        def devices():
            st, raw, _ = _request(op, f"{EA_API}/devices?{q}", headers=hdr)
            if st != 200:
                return {}
            devs = _device_list(_json(raw))
            if not devs:
                return {"geraete": 0}
            lst = [{"uuid": str(d.get("uuid", ""))[:100], "appid": str(d.get("appid") or "node_earnapp.com")[:60]}
                   for d in devs if d.get("uuid")]
            st, raw, _ = _request(op, f"{EA_API}/device_statuses?{q}", method="POST", headers=hdr, body={"list": lst})
            if st != 200:
                return _device_info(devs)
            stat = _json(raw).get("statuses")
            online = sum(1 for v in stat.values() if isinstance(v, dict) and v.get("online")) if isinstance(stat, dict) else None
            return _device_info(devs, online)
        return Result("earnapp", round(float(bal), 4), info=_safe_devices(devices))
    except CollectError as e:
        return Result("earnapp", None, str(e))


# --- Grass ----------------------------------------------------------------
GRASS_API = "https://api.getgrass.io"


def collect_grass(creds: dict) -> Result:
    token = creds.get("access_token", "").strip().strip('"')
    if not token:
        return Result("grass", None, "Kein Grass-Token hinterlegt")
    op = _opener()
    hdr = {"Authorization": token, "Origin": "https://app.grass.io", "Referer": "https://app.grass.io/"}
    try:
        st, raw, _ = _request(op, f"{GRASS_API}/retrieveUser", headers=hdr)
        if st in (401, 403):
            raise CollectError("Token abgelaufen – neuen Token aus app.grass.io holen")
        if st == 429:
            raise CollectError("Grass bremst gerade (Rate-Limit) – nächster Versuch beim nächsten Abruf")
        if st != 200:
            raise CollectError(f"Abruf fehlgeschlagen (HTTP {st})")
        user = _json(raw).get("result", {}).get("data", {}) or {}
        settled = float(user.get("totalPoints") or 0)
        if settled > 0:
            def devices():
                st, raw, _ = _request(op, f"{GRASS_API}/activeDevices", headers=hdr)
                if st != 200:
                    return {}
                n = len(_device_list((_json(raw).get("result") or {})))
                return {"geraete": n, "online": n}
            return Result("grass", round(settled, 2), info={"art": "abgerechnet", **_safe_devices(devices)})

        # Laufende Epoche: Punkte aus Geräte-Laufzeit schätzen (wie das Grass-Dashboard)
        st, raw, _ = _request(op, f"{GRASS_API}/activeDevices", headers=hdr)
        if st != 200:
            raise CollectError(f"Geräte-Abruf fehlgeschlagen (HTTP {st})")
        result = _json(raw).get("result")
        if not isinstance(result, dict) or "data" not in result:
            raise CollectError("Unerwartete Antwort von /activeDevices – API geändert?")
        pts = 0.0
        for d in result["data"] or []:
            up, score, mult = float(d.get("aggUptime", 0)), float(d.get("ipScore", 0)), float(d.get("multiplier", 1))
            if up > 0 and score > 0:
                pts += up / 3600 * 50 * (score / 100) * mult
        n = len(result["data"] or [])
        return Result("grass", round(pts, 2), info={"art": "geschätzt (laufende Epoche)", "geraete": n, "online": n})
    except CollectError as e:
        return Result("grass", None, str(e))


# --- Bytelixir ------------------------------------------------------------
BL_BASE = "https://dash.bytelixir.com"
BL_REMEMBER = "remember_web_59ba36addc2b2f9401580f014c7f58ea4e30989d"
# Guthaben steht in der hervorgehobenen Zeile (Klasse "text-primary").
# Format seit 10/2026:  <span>$</span><span>0.39</span><span class="text-xs">extra</span>
_BL_BAL = re.compile(r'text-primary[^>]*>\s*<span>\$</span>\s*<span>(\d[\d,]*(?:\.\d+)?)</span>(?:\s*<span[^>]*>(\d*)</span>)?')
# Fallback: irgendein "$<span>Zahl</span>" auf der Seite
_BL_RE = re.compile(r'<span>\$</span>\s*<span>(\d[\d,]*(?:\.\d+)?)</span>(?:\s*<span[^>]*>(\d*)</span>)?')
# Fallback: ganz altes Format, Zahl ohne eigenes span
_BL_OLD = re.compile(r'<span>\$</span>(\d[\d,]*\.\d+)<span[^>]*>(\d*)</span>')


def collect_bytelixir(creds: dict) -> Result:
    sess = urllib.parse.unquote(creds.get("session", ""))
    if not sess and not creds.get("remember_web"):
        return Result("bytelixir", None, "Keine Bytelixir-Cookies hinterlegt")
    jar = http.cookiejar.CookieJar()
    dom = "dash.bytelixir.com"
    if sess:
        jar.set_cookie(_cookie("bytelixir_session", sess, dom))
    if creds.get("remember_web"):
        jar.set_cookie(_cookie(BL_REMEMBER, urllib.parse.unquote(creds["remember_web"]), dom))
    if creds.get("xsrf"):
        jar.set_cookie(_cookie("XSRF-TOKEN", urllib.parse.unquote(creds["xsrf"]), dom))
    op = _opener(jar)
    try:
        st, raw, final = _request(op, f"{BL_BASE}/", accept="text/html")
        if urllib.parse.urlparse(final).path.startswith("/login") or st in (401, 419):
            raise CollectError("Sitzung abgelaufen – Cookies neu kopieren (beim Login 'Remember Me' anhaken)")
        if st != 200:
            raise CollectError(f"Dashboard-Abruf fehlgeschlagen (HTTP {st})")
        html = raw.decode("utf-8", "replace")
        vals = []
        for main, extra in (_BL_BAL.findall(html) or _BL_RE.findall(html) or _BL_OLD.findall(html)):
            try:
                vals.append(float(main.replace(",", "") + (extra or "")))
            except ValueError:
                pass
        if not vals:
            raise CollectError("Kontostand nicht gefunden – Seite geändert?")
        bal = next((v for v in vals if v > 0), vals[0])
        # Laravel erneuert das Sitzungs-Cookie bei jedem Aufruf -> neuen Wert merken
        new = dict(creds)
        for c in jar:
            if c.name == "bytelixir_session":
                new["session"] = c.value
            elif c.name == "XSRF-TOKEN":
                new["xsrf"] = c.value
        return Result("bytelixir", round(bal, 6), new_creds=new if new != creds else None)
    except CollectError as e:
        return Result("bytelixir", None, str(e))


# --- Duino-Coin -----------------------------------------------------------
DUCO_API = "https://server.duinocoin.com"
_USER_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


def collect_duinocoin(creds: dict) -> Result:
    user = creds.get("username", "").strip()
    if not _USER_RE.match(user):
        return Result("duinocoin", None, "Ungültiger Duino-Coin-Benutzername")
    op = _opener()
    try:
        st, raw, _ = _request(op, f"{DUCO_API}/users/{urllib.parse.quote(user)}")
        if st != 200:
            raise CollectError(f"Abruf fehlgeschlagen (HTTP {st})")
        data = _json(raw)
        if not data.get("success"):
            raise CollectError("Benutzer nicht gefunden")
        res = data.get("result", {})
        bal = res.get("balance", {}).get("balance")
        if bal is None:
            raise CollectError("Feld balance fehlt – API geändert?")
        miners = res.get("miners") or []
        hashrate = sum(float(m.get("hashrate") or 0) for m in miners)
        price = None
        try:
            st2, raw2, _ = _request(op, f"{DUCO_API}/api.json")
            if st2 == 200:
                price = float(_json(raw2).get("Duco price") or 0) or None
        except CollectError:
            pass
        rigs = []
        for m in miners[:24]:
            name = str(m.get("identifier") or m.get("software") or "Miner")[:24]
            rigs.append({"name": name, "hashrate": float(m.get("hashrate") or 0)})
        return Result("duinocoin", round(float(bal), 6), price_usd=price,
                      info={"miner": len(miners), "hashrate": hashrate, "rigs": rigs,
                            "geraete": len(miners), "online": len(miners)})
    except CollectError as e:
        return Result("duinocoin", None, str(e))


# --- Pawns.app (IPRoyal) --------------------------------------------------
PAWNS_API = "https://api.pawns.app/api/v1"
_pawns_token_cache: dict[str, str] = {}


def collect_pawns(creds: dict) -> Result:
    import secrets, string
    email, password = creds.get("email", ""), creds.get("password", "")
    if not email or not password:
        return Result("pawns", None, "Keine Zugangsdaten hinterlegt")
    op = _opener()
    try:
        def login() -> str:
            ident = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(21))
            st, raw, _ = _request(op, f"{PAWNS_API}/users/tokens", method="POST",
                                  body={"identifier": ident, "email": email, "password": password,
                                        "h_captcha_response": ""})
            if st in (400, 401, 403, 422):
                raise CollectError("Login abgelehnt – E-Mail/Passwort prüfen (oder Captcha verlangt)")
            if st == 429:
                raise CollectError("Pawns bremst gerade (Rate-Limit) – nächster Versuch später")
            if st not in (200, 201):
                raise CollectError(f"Login fehlgeschlagen (HTTP {st})")
            tok = _json(raw).get("access_token")
            if not tok:
                raise CollectError("Kein Token in der Login-Antwort")
            return tok

        token = _pawns_token_cache.get(email) or login()
        st, raw, _ = _request(op, f"{PAWNS_API}/users/me/balance-dashboard",
                              headers={"Authorization": f"Bearer {token}"})
        if st == 401:
            token = login()
            st, raw, _ = _request(op, f"{PAWNS_API}/users/me/balance-dashboard",
                                  headers={"Authorization": f"Bearer {token}"})
        if st != 200:
            raise CollectError(f"Kontostand-Abruf fehlgeschlagen (HTTP {st})")
        _pawns_token_cache[email] = token
        bal = _json(raw).get("balance")
        if bal is None:
            raise CollectError("Feld balance fehlt – API geändert?")

        def devices():
            st, raw, _ = _request(op, f"{PAWNS_API}/users/me/devices?page=1&items_per_page=100",
                                  headers={"Authorization": f"Bearer {token}"})
            return _device_info(_device_list(_json(raw))) if st == 200 else {}
        return Result("pawns", round(float(bal), 4), info=_safe_devices(devices))
    except CollectError as e:
        _pawns_token_cache.pop(email, None)
        return Result("pawns", None, str(e))


# --- Earn.fm --------------------------------------------------------------
EFM_AUTH = "https://sb.earn.fm/auth/v1/token?grant_type=password"
EFM_API = "https://api.earn.fm/v2"
# Öffentlicher "anon"-Schlüssel aus der Earn.fm-Webseite (kein Geheimnis)
EFM_ANON = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "ewogICJyb2xlIjogImFub24iLAogICJpc3MiOiAic3VwYWJhc2UiLAogICJpYXQiOiAxNjkyNjU1MjAwLAogICJleHAiOiAxODUwNTA4MDAwCn0."
            "jp-Uj5ro0jj7MHnlE8HHZRsZAFOI1d_T9n_9tnE09vM")


def collect_earnfm(creds: dict) -> Result:
    email, password = creds.get("email", "").strip(), creds.get("password", "")
    if not email or not password:
        return Result("earnfm", None, "Keine Zugangsdaten hinterlegt")
    op = _opener()
    try:
        st, raw, _ = _request(op, EFM_AUTH, method="POST", body={"email": email, "password": password},
                              headers={"apikey": EFM_ANON})
        if st in (400, 401, 403):
            raise CollectError("Login abgelehnt – E-Mail/Passwort prüfen")
        if st != 200:
            raise CollectError(f"Login fehlgeschlagen (HTTP {st})")
        token = _json(raw).get("access_token")
        if not token:
            raise CollectError("Kein Token in der Login-Antwort")
        st, raw, _ = _request(op, f"{EFM_API}/harvester/view_balance", headers={"X-API-Key": token})
        if st in (401, 403):
            raise CollectError("Token abgelehnt – Zugangsdaten prüfen")
        if st != 200:
            raise CollectError(f"Kontostand-Abruf fehlgeschlagen (HTTP {st})")
        bal = (_json(raw).get("data") or {}).get("totalBalance")
        if bal is None:
            raise CollectError("Feld totalBalance fehlt – API geändert?")
        return Result("earnfm", round(float(bal), 4))
    except CollectError as e:
        return Result("earnfm", None, str(e))


# --- Registry -------------------------------------------------------------
# unit: Einheit des Guthabens. usd: True = Einheit ist USD; "price" = Kurs wird
# mit abgerufen; None = kein Geldwert (Punkte).
# on_drop: was ein sinkender Stand bedeutet: "payout" (Auszahlung) oder
# "reset" (neue Epoche beginnt bei 0, z. B. Grass).
PLATFORMS = {
    "honeygain": {
        "name": "Honeygain", "color": "#F5A623", "short": "HG", "unit": "USD", "usd": True, "on_drop": "payout",
        "collect": collect_honeygain, "dashboard": "https://dashboard.honeygain.com/", "min_payout": 20.0,
        "fields": [
            {"key": "email", "label": "E-Mail", "type": "email", "required": True},
            {"key": "password", "label": "Passwort", "type": "password", "required": True},
        ],
        "hint": "Gleiche Daten wie auf dashboard.honeygain.com. Bei Google/Apple-Login vorher ein Passwort setzen.",
    },
    "honeygain_jt": {
        "name": "Honeygain (JumpTask)", "color": "#4F8CFF", "short": "JT", "unit": "USD", "usd": True, "on_drop": "payout",
        "collect": collect_honeygain_jt, "fallback_creds": "honeygain", "lifetime": True,
        "dashboard": "https://app.jumptask.io/", "min_payout": None,
        "fields": [
            {"key": "email", "label": "E-Mail des JumpTask-Kontos (optional)", "type": "email", "required": False},
            {"key": "password", "label": "Passwort (optional)", "type": "password", "required": False},
        ],
        "hint": "Eigener Honeygain-Login für das Konto im JumpTask-Modus. Leer lassen, wenn du nur EIN Honeygain-Konto hast – dann wird der normale Honeygain-Zugang benutzt.",
    },
    "earnapp": {
        "name": "EarnApp", "color": "#34C38F", "short": "EA", "unit": "USD", "usd": True, "on_drop": "payout",
        "collect": collect_earnapp, "dashboard": "https://earnapp.com/dashboard", "min_payout": 10.0,
        "fields": [
            {"key": "oauth_token", "label": "oauth-refresh-token", "type": "password", "required": True},
            {"key": "brd_sess_id", "label": "brd_sess_id (optional)", "type": "password", "required": False},
        ],
        "hint": "earnapp.com einloggen → F12 → Anwendung/Speicher → Cookies → oauth-refresh-token kopieren.",
    },
    "grass": {
        "name": "Grass", "color": "#86EFAC", "short": "GR", "unit": "Punkte", "usd": None, "on_drop": "reset",
        "collect": collect_grass, "dashboard": "https://app.grass.io/dashboard", "min_payout": None,
        "fields": [
            {"key": "access_token", "label": "Access-Token", "type": "password", "required": True},
        ],
        "hint": "app.grass.io einloggen → F12 → Anwendung → Lokaler Speicher → https://app.grass.io → Wert von 'accessToken' (bzw. 'token') kopieren. Grass zahlt Punkte, keinen festen Dollarbetrag.",
    },
    "bytelixir": {
        "name": "Bytelixir", "color": "#A78BFA", "short": "BX", "unit": "USD", "usd": True, "on_drop": "payout",
        "collect": collect_bytelixir, "dashboard": "https://dash.bytelixir.com/", "min_payout": None,
        "fields": [
            {"key": "session", "label": "bytelixir_session", "type": "password", "required": False},
            {"key": "remember_web", "label": "remember_web_… (wichtig, hält ~1 Jahr)", "type": "password", "required": True},
            {"key": "xsrf", "label": "XSRF-TOKEN (optional)", "type": "password", "required": False},
        ],
        "hint": "dash.bytelixir.com mit 'Remember Me' einloggen → F12 → Anwendung → Cookies → https://dash.bytelixir.com → die drei Werte kopieren.",
    },
    "duinocoin": {
        "name": "Duino-Coin", "color": "#FB923C", "short": "DC", "unit": "DUCO", "usd": "price", "on_drop": "payout",
        "collect": collect_duinocoin, "dashboard": "https://wallet.duinocoin.com/", "min_payout": None,
        "fields": [
            {"key": "username", "label": "Duino-Coin-Benutzername", "type": "text", "required": True},
        ],
        "hint": "Nur der Benutzername – Duino-Coin-Kontostände sind öffentlich, es wird kein Passwort gebraucht. Zeigt auch deine aktiven Miner (z. B. ESP32) und die Hashrate.",
    },
    "pawns": {
        "name": "Pawns.app", "color": "#F472B6", "short": "PA", "unit": "USD", "usd": True, "on_drop": "payout",
        "collect": collect_pawns, "dashboard": "https://dashboard.pawns.app/", "min_payout": 5.0,
        "fields": [
            {"key": "email", "label": "E-Mail", "type": "email", "required": True},
            {"key": "password", "label": "Passwort", "type": "password", "required": True},
        ],
        "hint": "Gleiche Daten wie auf pawns.app. Bei Google-Login vorher ein Passwort setzen.",
    },
    "earnfm": {
        "name": "Earn.fm", "color": "#22D3EE", "short": "FM", "unit": "USD", "usd": True, "on_drop": "payout",
        "collect": collect_earnfm, "dashboard": "https://app.earn.fm/", "min_payout": None,
        "fields": [
            {"key": "email", "label": "E-Mail", "type": "email", "required": True},
            {"key": "password", "label": "Passwort", "type": "password", "required": True},
        ],
        "hint": "Gleiche Daten wie auf app.earn.fm (Login mit E-Mail und Passwort).",
    },
    "wipter": {
        "name": "Wipter", "color": "#FACC15", "short": "WI", "unit": "USD", "usd": True, "on_drop": "payout",
        "collect": None, "manual": True, "dashboard": "https://wipter.com/", "min_payout": None,
        "fields": [
            {"key": "balance", "label": "Aktueller Kontostand in $", "type": "text", "required": True},
        ],
        "hint": "Wipter hat keine Schnittstelle zum Abrufen. Trag den Kontostand von wipter.com hier ab und zu ein – EarnWatch rechnet daraus deine Einnahmen.",
    },
}
