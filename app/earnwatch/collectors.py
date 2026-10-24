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

    @property
    def ok(self) -> bool:
        return self.balance is not None


class CollectError(Exception):
    pass


def _opener(jar: http.cookiejar.CookieJar | None = None):
    handlers = [urllib.request.HTTPSHandler(context=_SSL)]
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


# --- Honeygain ------------------------------------------------------------
HG_API = "https://dashboard.honeygain.com/api"
_hg_token_cache: dict[str, str] = {}


def collect_honeygain(creds: dict) -> list[Result]:
    """Liefert zwei Werte: normales Honeygain-Guthaben und (falls genutzt) JumpTask-Einnahmen."""
    email, password = creds.get("email", ""), creds.get("password", "")
    if not email or not password:
        return [Result("honeygain", None, "Keine Zugangsdaten hinterlegt")]
    op = _opener()
    hdr = {"Referer": "https://dashboard.honeygain.com/"}
    try:
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

        token = _hg_token_cache.get(email) or login()

        def get(path: str):
            nonlocal token
            st, raw, _ = _request(op, f"{HG_API}{path}", headers={**hdr, "Authorization": f"Bearer {token}"})
            if st == 401:
                token = login()
                st, raw, _ = _request(op, f"{HG_API}{path}", headers={**hdr, "Authorization": f"Bearer {token}"})
            return st, raw

        st, raw = get("/v1/users/balances")
        if st != 200:
            raise CollectError(f"Kontostand-Abruf fehlgeschlagen (HTTP {st})")
        _hg_token_cache[email] = token
        cents = _json(raw).get("data", {}).get("payout", {}).get("usd_cents")
        if cents is None:
            raise CollectError("Feld usd_cents fehlt – API geändert?")

        # JumpTask-Modus: an oder aus?
        jt_on = None
        st_me, raw_me = get("/v1/users/me")
        if st_me == 200:
            jt_on = bool(_json(raw_me).get("data", {}).get("jt_toggle"))
        mode = {"modus": "JumpTask" if jt_on else "Honeygain"} if jt_on is not None else {}
        results = [Result("honeygain", round(float(cents) / 100, 4), info=mode)]

        # JumpTask-Einnahmen (Lebenszeit, in USD-Cent) – wächst nur, Auszahlung läuft über JumpTask
        st_jt, raw_jt = get("/v1/earnings/jt")
        if st_jt == 200:
            d = _json(raw_jt).get("data", {}) or {}
            jt_cents = float(d.get("total_usd_cents") or 0) + float(d.get("bonus_usd_cents") or 0)
            jt_credits = float(d.get("total_credits") or 0) + float(d.get("bonus_credits") or 0)
            results.append(Result("honeygain_jt", round(jt_cents / 100, 4),
                                  info={**mode, "credits": round(jt_credits, 2)}))
        elif jt_on:
            results.append(Result("honeygain_jt", None, f"JumpTask-Abruf fehlgeschlagen (HTTP {st_jt})"))
        return results
    except CollectError as e:
        _hg_token_cache.pop(email, None)
        return [Result("honeygain", None, str(e))]


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
        return Result("earnapp", round(float(bal), 4))
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
            return Result("grass", round(settled, 2), info={"art": "abgerechnet"})

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
        return Result("grass", round(pts, 2), info={"art": "geschätzt (laufende Epoche)",
                                                     "geraete": len(result["data"] or [])})
    except CollectError as e:
        return Result("grass", None, str(e))


# --- Bytelixir ------------------------------------------------------------
BL_BASE = "https://dash.bytelixir.com"
BL_REMEMBER = "remember_web_59ba36addc2b2f9401580f014c7f58ea4e30989d"
_BL_RE = re.compile(r"<span>\$</span>(\d+\.\d+)<span[^>]*>(\d*)</span>")


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
        for main, extra in _BL_RE.findall(html):
            try:
                vals.append(float(main + extra))
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
        return Result("duinocoin", round(float(bal), 6), price_usd=price,
                      info={"miner": len(miners), "hashrate": hashrate})
    except CollectError as e:
        return Result("duinocoin", None, str(e))


# --- Registry -------------------------------------------------------------
# unit: Einheit des Guthabens. usd: True = Einheit ist USD; "price" = Kurs wird
# mit abgerufen; None = kein Geldwert (Punkte).
# on_drop: was ein sinkender Stand bedeutet: "payout" (Auszahlung) oder
# "reset" (neue Epoche beginnt bei 0, z. B. Grass).
PLATFORMS = {
    "honeygain": {
        "name": "Honeygain", "color": "#f5a623", "unit": "USD", "usd": True, "on_drop": "payout",
        "collect": collect_honeygain, "dashboard": "https://dashboard.honeygain.com/", "min_payout": 20.0,
        "fields": [
            {"key": "email", "label": "E-Mail", "type": "email", "required": True},
            {"key": "password", "label": "Passwort", "type": "password", "required": True},
        ],
        "hint": "Gleiche Daten wie auf dashboard.honeygain.com. Bei Google/Apple-Login vorher ein Passwort setzen.",
    },
    "honeygain_jt": {
        "name": "Honeygain (JumpTask)", "color": "#3b82f6", "unit": "USD", "usd": True, "on_drop": "payout",
        "collect": None, "linked": "honeygain", "lifetime": True, "dashboard": "https://app.jumptask.io/", "min_payout": None,
        "fields": [],
        "hint": "Nutzt automatisch den Honeygain-Zugang. Zeigt, was Honeygain im JumpTask-Modus an JumpTask ausgezahlt hat.",
    },
    "earnapp": {
        "name": "EarnApp", "color": "#3fb68b", "unit": "USD", "usd": True, "on_drop": "payout",
        "collect": collect_earnapp, "dashboard": "https://earnapp.com/dashboard", "min_payout": 10.0,
        "fields": [
            {"key": "oauth_token", "label": "oauth-refresh-token", "type": "password", "required": True},
            {"key": "brd_sess_id", "label": "brd_sess_id (optional)", "type": "password", "required": False},
        ],
        "hint": "earnapp.com einloggen → F12 → Anwendung/Speicher → Cookies → oauth-refresh-token kopieren.",
    },
    "grass": {
        "name": "Grass", "color": "#a3e635", "unit": "Punkte", "usd": None, "on_drop": "reset",
        "collect": collect_grass, "dashboard": "https://app.grass.io/dashboard", "min_payout": None,
        "fields": [
            {"key": "access_token", "label": "Access-Token", "type": "password", "required": True},
        ],
        "hint": "app.grass.io einloggen → F12 → Anwendung → Lokaler Speicher → https://app.grass.io → Wert von 'accessToken' (bzw. 'token') kopieren. Grass zahlt Punkte, keinen festen Dollarbetrag.",
    },
    "bytelixir": {
        "name": "Bytelixir", "color": "#8b5cf6", "unit": "USD", "usd": True, "on_drop": "payout",
        "collect": collect_bytelixir, "dashboard": "https://dash.bytelixir.com/", "min_payout": None,
        "fields": [
            {"key": "session", "label": "bytelixir_session", "type": "password", "required": False},
            {"key": "remember_web", "label": "remember_web_… (wichtig, hält ~1 Jahr)", "type": "password", "required": True},
            {"key": "xsrf", "label": "XSRF-TOKEN (optional)", "type": "password", "required": False},
        ],
        "hint": "dash.bytelixir.com mit 'Remember Me' einloggen → F12 → Anwendung → Cookies → https://dash.bytelixir.com → die drei Werte kopieren.",
    },
    "duinocoin": {
        "name": "Duino-Coin", "color": "#f97316", "unit": "DUCO", "usd": "price", "on_drop": "payout",
        "collect": collect_duinocoin, "dashboard": "https://wallet.duinocoin.com/", "min_payout": None,
        "fields": [
            {"key": "username", "label": "Duino-Coin-Benutzername", "type": "text", "required": True},
        ],
        "hint": "Nur der Benutzername – Duino-Coin-Kontostände sind öffentlich, es wird kein Passwort gebraucht. Zeigt auch deine aktiven Miner (z. B. ESP32) und die Hashrate.",
    },
}
