"""EarnWatch – sicheres Einnahmen-Dashboard für Honeygain & EarnApp."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import secrets
import csv
import datetime as dt
import io
import json
import logging
import math
import mimetypes
import time
from pathlib import Path
from urllib.parse import urlparse

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from markupsafe import Markup

from . import achievements, auth, webauthn, charts, config, db, events, fx, notify, stats, totp
from .collectors import PLATFORMS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("earnwatch")

mimetypes.add_type("application/manifest+json", ".webmanifest")
mimetypes.add_type("font/woff", ".woff")

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
templates.env.globals["money"] = charts.money
templates.env.globals["amount"] = charts.amount
templates.env.globals["cur_label"] = lambda: "Euro" if charts.currency()[0] == "EUR" else "USD"


def _ago(ts):
    if not ts:
        return "–"
    d = int(time.time() - ts)
    if d < 90:
        return "gerade eben"
    if d < 3600:
        return f"vor {d // 60} Min"
    if d < 86400:
        return f"vor {d // 3600} Std"
    if d < 2 * 86400:
        return "gestern"
    return dt.datetime.fromtimestamp(ts, stats.TZ).strftime("%d.%m.%Y")


templates.env.filters["ago"] = _ago
templates.env.filters["dtfmt"] = lambda ts: dt.datetime.fromtimestamp(ts, stats.TZ).strftime("%d.%m.%Y %H:%M") if ts else "–"

_collect_lock = asyncio.Lock()
_last_manual = 0.0


# --------------------------------------------------------------------------
# Sicherheits-Header
# --------------------------------------------------------------------------
CSP = (
    "default-src 'none'; style-src 'self'; img-src 'self'; font-src 'self'; script-src 'none'; "
    "form-action 'self'; manifest-src 'self'; frame-ancestors 'none'; base-uri 'none'"
)


# Nur Login- und Einstellungsseite dürfen das eigene Passkey-Skript laden (Face ID braucht JavaScript).
CSP_PASSKEY = CSP.replace("script-src 'none'", "script-src 'self'; connect-src 'self'")


class SecurityHeaders:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        # Anfragen größer als 64 KB gar nicht erst annehmen
        for k, v in scope.get("headers", []):
            if k == b"content-length" and (not v.isdigit() or int(v) > 65536):
                await send({"type": "http.response.start", "status": 413,
                            "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
                await send({"type": "http.response.body", "body": "Anfrage zu groß".encode()})
                return
        received = 0
        is_https = scope.get("scheme") == "https" or any(
            k == b"x-forwarded-proto" and v == b"https" for k, v in scope.get("headers", []))

        async def _receive():
            nonlocal received
            msg = await receive()
            if msg["type"] == "http.request":
                received += len(msg.get("body", b""))
                if received > 65536:
                    raise RuntimeError("Anfrage zu groß")
            return msg

        async def _send(msg):
            if msg["type"] == "http.response.start":
                h = msg.setdefault("headers", [])
                if is_https:  # nur über HTTPS: Browser soll nie wieder unverschlüsselt fragen
                    h.append((b"strict-transport-security", b"max-age=31536000"))
                for k, v in (
                    ("content-security-policy", CSP_PASSKEY if scope.get("path") in ("/login", "/settings") else CSP),
                    ("x-content-type-options", "nosniff"),
                    ("x-frame-options", "DENY"),
                    ("referrer-policy", "no-referrer"),
                    ("permissions-policy", "camera=(), microphone=(), geolocation=(), interest-cohort=()"),
                    ("cross-origin-opener-policy", "same-origin"),
                    ("cache-control", "no-store"),
                ):
                    h.append((k.encode(), v.encode()))
            await send(msg)

        await self.app(scope, _receive, _send)


# --------------------------------------------------------------------------
# Hilfsfunktionen
# --------------------------------------------------------------------------
def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def current_user(request: Request):
    uid = request.session.get("uid")
    if not uid:
        return None
    user = db.get_user(uid)
    if not user or request.session.get("ver") != user["session_ver"]:
        request.session.clear()
        return None
    # Gleitendes Ablaufen: wer die Seite benutzt, bleibt angemeldet. Abgemeldet wird nach
    # Inaktivität (12 h bzw. 30 Tage mit „Angemeldet bleiben“) und spätestens nach 7 / 90 Tagen.
    now = time.time()
    long_ = request.session.get("long")
    idle = config.REMEMBER_DAYS * 86400 if long_ else config.SESSION_HOURS * 3600
    absolute = (90 if long_ else 7) * 86400
    since = request.session.get("since", 0)
    seen = request.session.get("seen", since)
    if now - seen > idle or now - since > absolute:
        request.session.clear()
        return None
    if now - seen > 60:
        request.session["seen"] = int(now)
    return user


def render(request: Request, name: str, ctx: dict | None = None, status: int = 200):
    ctx = dict(ctx or {})
    ctx["csrf"] = auth.csrf_token(request.session)
    ctx.setdefault("user", None)
    ctx["flash"] = request.session.pop("flash", None)
    ctx["theme"] = db.get_setting("theme", "auto") if db.user_count() else "auto"
    ctx["ui_style"] = db.get_setting("ui_style", "glass") if db.user_count() else "glass"
    if ctx["user"] and name in ("dashboard.html", "platform.html"):
        running = _collect_lock.locked() or any(not t.done() for t in _bg_tasks)
        waiting = request.session.get("await_collect")
        if running and waiting:
            ctx["collecting"] = True   # Seite lädt sich per <meta refresh> neu, bis der Abruf fertig ist
        elif waiting:
            request.session.pop("await_collect", None)
            try:
                res = json.loads(db.get_setting("manual_result", "{}"))
            except ValueError:
                res = {}
            if res.get("ts", 0) >= waiting and not ctx["flash"]:
                ctx["flash"] = {"msg": str(res.get("msg", ""))[:400], "kind": res.get("kind") if res.get("kind") in ("ok", "warn", "err") else "ok"}
    if ctx["user"]:
        interval = max(15, int(db.get_setting("interval_min", str(config.INTERVAL_MIN_DEFAULT))))
        last = int(db.get_setting("last_collect_ts", "0") or 0)
        nxt = max(0, int((last + interval * 60 - time.time()) / 60)) if last else None
        ctx["status"] = {"interval": interval, "next_in": nxt,
                         "frac": round(1 - nxt / interval, 3) if nxt is not None else 0}
        try:
            ct = json.loads(db.get_setting("containers", "{}"))
        except ValueError:
            ct = {}
        if ct.get("services") and time.time() - ct.get("ts", 0) < 20 * 60:
            svc = ct["services"]
            up = sum(1 for x in svc if x["state"] == "running" and x.get("health") != "unhealthy")
            ctx["status"]["containers"] = {"up": up, "total": len(svc),
                                           "down": [x["label"] for x in svc if x["state"] != "running" or x.get("health") == "unhealthy"]}
    return templates.TemplateResponse(request, name, ctx, status_code=status)


def _json_setting(key: str) -> dict:
    try:
        v = json.loads(db.get_setting(key, "{}"))
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def flash(request: Request, msg: str, kind: str = "ok"):
    request.session["flash"] = {"msg": msg, "kind": kind}


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


async def checked_form(request: Request):
    """Formular lesen + CSRF-Token und Origin prüfen. Gibt None bei Angriff zurück."""
    origin = request.headers.get("origin")
    if origin and origin != "null":
        # Hinter einem Proxy (Caddy, Tailscale) steht der echte Name ggf. in X-Forwarded-Host.
        # Ein Angreifer-Formular kann keine eigenen Header setzen – das bleibt sicher.
        hosts = {request.headers.get("host"), request.headers.get("x-forwarded-host")}
        if urlparse(origin).netloc not in hosts:
            return None
    form = await request.form(max_files=0, max_fields=20, max_part_size=8192)
    if not auth.csrf_ok(request.session, form.get("csrf")):
        return None
    return form


def forbidden():
    return PlainTextResponse("Ungültige Anfrage (CSRF-Schutz). Seite neu laden.", status_code=403)


# --------------------------------------------------------------------------
# Abruf-Logik
# --------------------------------------------------------------------------
async def collect_all() -> list:
    results = []
    async with _collect_lock:
        db.set_setting("last_collect_ts", str(int(time.time())))
        runs_before = db.last_runs()
        for pf, meta in PLATFORMS.items():
            if meta.get("collect") is None:
                continue  # wird von einer anderen Plattform mit abgerufen (z. B. JumpTask)
            creds = db.load_credentials(pf)
            if not creds or not any(creds.values()):
                creds = db.load_credentials(meta["fallback_creds"]) if meta.get("fallback_creds") else None
            if creds is None:
                continue
            try:
                out = await run_in_threadpool(meta["collect"], creds)
            except Exception:  # Sicherheitsnetz: nie Details (evtl. Geheimnisse) loggen
                log.error("%s: unerwarteter Fehler im Abruf", meta["name"])
                db.add_run(pf, False, "Unerwarteter Fehler beim Abruf")
                continue
            for res in (out if isinstance(out, list) else [out]):
                rmeta = PLATFORMS.get(res.platform)
                if rmeta is None:
                    continue
                if res.ok and not (math.isfinite(res.balance) and -1e9 < res.balance < 1e9):
                    res.balance, res.message = None, "Unplausibler Wert vom Anbieter verworfen"
                if res.price_usd is not None and not (math.isfinite(res.price_usd) and 0 < res.price_usd < 1e6):
                    res.price_usd = None
                if res.ok and res.history:
                    db.save_history(res.platform, res.history)
                events.after_result(res, rmeta, runs_before.get(res.platform))
                if res.ok:
                    db.add_snapshot(res.platform, res.balance, rmeta["unit"])
                    db.add_run(res.platform, True, None)
                    if res.price_usd:
                        db.set_setting(f"price_{res.platform}", str(res.price_usd))
                    db.set_setting(f"info_{res.platform}", json.dumps(res.info))
                    if res.new_creds:
                        db.save_credentials(res.platform, res.new_creds)
                    log.info("%s: Stand %.6f %s", rmeta["name"], res.balance, rmeta["unit"])
                else:
                    db.add_run(res.platform, False, res.message)
                    log.warning("%s: %s", rmeta["name"], res.message)
                results.append(res)
        try:
            achievements.evaluate(stats.compute())
        except Exception:
            log.exception("Fehler bei den Erfolgen")
    return results


async def scheduler():
    await asyncio.sleep(5)
    while True:
        try:
            await collect_all()
        except Exception:
            log.exception("Fehler im Abruf-Zyklus")
        minutes = max(15, int(db.get_setting("interval_min", str(config.INTERVAL_MIN_DEFAULT))))
        await asyncio.sleep(minutes * 60)


# --------------------------------------------------------------------------
# Seiten
# --------------------------------------------------------------------------
async def setup(request: Request):
    if db.user_count() > 0:
        return redirect("/login")
    error = None
    if request.method == "POST":
        ip = client_ip(request)
        form = await checked_form(request)
        if form is None:
            return forbidden()
        uname = form.get("username", "").strip()
        # Bremse pro IP UND pro Benutzername: auch mit wechselnden (gefälschten) IP-Adressen
        # bleiben es höchstens 5 Passwort-Versuche pro Konto in 15 Minuten.
        if wait := (auth.throttle.try_acquire(ip)
                    or auth.throttle.try_acquire("user:" + uname.lower()[:64], global_=False)):
            return render(request, "setup.html", {"error": f"Zu viele Versuche. Bitte {wait//60+1} Min. warten."}, 429)
        token = form.get("token", "")
        username = form.get("username", "").strip()
        pw, pw2 = form.get("password", ""), form.get("password2", "")
        if not hmac.compare_digest(token.encode(), config.setup_token().encode()):
            error = "Einrichtungs-Token ist falsch."
        elif not (3 <= len(username) <= 32 and username.isalnum()):
            error = "Benutzername: 3–32 Buchstaben/Ziffern."
        elif pw != pw2:
            error = "Die Passwörter stimmen nicht überein."
        elif problem := auth.password_problems(pw):
            error = problem
        else:
            if not db.create_first_user(username, await run_in_threadpool(auth.hash_password, pw)):
                return redirect("/login")
            log.info("Admin-Konto '%s' angelegt", username)
            flash(request, "Konto angelegt – bitte anmelden.")
            return redirect("/login")
    return render(request, "setup.html", {"error": error})


async def login(request: Request):
    if db.user_count() == 0:
        return redirect("/setup")
    if current_user(request):
        return redirect("/")
    error = None
    status = 200
    if request.method == "POST":
        ip = client_ip(request)
        form = await checked_form(request)
        if form is None:
            return forbidden()
        uname = form.get("username", "").strip()
        # Bremse pro IP UND pro Benutzername: auch mit wechselnden (gefälschten) IP-Adressen
        # bleiben es höchstens 5 Passwort-Versuche pro Konto in 15 Minuten.
        if wait := (auth.throttle.try_acquire(ip)
                    or auth.throttle.try_acquire("user:" + uname.lower()[:64], global_=False)):
            notify.send("security", "🚨 Viele fehlgeschlagene Logins",
                        f"Von IP {ip} wurde der Login gesperrt (zu viele falsche Passwörter).",
                        priority=4, tags=["rotating_light"], dedupe=f"lock:{ip}", dedupe_seconds=900)
            return render(request, "login.html", {"error": f"Zu viele Fehlversuche. Bitte {wait//60+1} Min. warten."}, 429)
        user = db.get_user_by_name(uname)
        pw = form.get("password", "")
        stored = user["pw_hash"] if user else auth.DUMMY_HASH
        ok = await run_in_threadpool(auth.verify_password, pw, stored)
        if user and ok:
            auth.throttle.success("user:" + uname.lower()[:64])
            request.session.clear()  # neue Session (gegen Session-Fixation)
            if db.totp_secret(user):
                # Passwort stimmt – jetzt noch der zweite Faktor (max. 5 Minuten Zeit)
                request.session.update({"pre_uid": user["id"], "pre_ver": user["session_ver"],
                                        "pre_ts": int(time.time()), "pre_long": form.get("remember") == "1"})
                return redirect("/login/2fa")
            auth.throttle.success(ip)
            request.session.update({"uid": user["id"], "ver": user["session_ver"], "since": int(time.time()),
                                    "long": form.get("remember") == "1"})
            log.info("Login '%s' von %s", user["username"], ip)
            notify.send("login", "🔐 Neue Anmeldung bei EarnWatch",
                        f"Benutzer {user['username']} · {notify.short_agent(request.headers.get('user-agent', ''))} · IP {ip}",
                        priority=3, tags=["lock"], dedupe=f"login:{ip}", dedupe_seconds=60)
            return redirect("/")
        log.warning("Fehlgeschlagener Login von %s", ip)
        error, status = "Benutzername oder Passwort falsch.", 401
    return render(request, "login.html", {"error": error}, status)


# --------------------------------------------------------------------------
# Zwei-Faktor-Anmeldung
# --------------------------------------------------------------------------
def _pending_login_user(request: Request):
    uid, ts = request.session.get("pre_uid"), request.session.get("pre_ts", 0)
    if not uid or time.time() - ts > 300:
        return None
    user = db.get_user(uid)
    if not user or user["session_ver"] != request.session.get("pre_ver") or not db.totp_secret(user):
        return None
    return user


def _check_second_factor(user, code: str) -> bool:
    """TOTP-Code oder Wiederherstellungscode prüfen (jeder nur einmal gültig)."""
    secret = db.totp_secret(user)
    if not secret:
        return False
    counter = totp.verify(secret, code, user["totp_last"])
    if counter is not None:
        return db.set_totp_last(user["id"], counter)  # atomar: parallele Wiederverwendung scheitert
    raw = user["recovery"] or "[]"
    rest = totp.use_recovery(code, db.recovery_hashes(user))
    if rest is not None and db.set_recovery(user["id"], rest, expect_old=raw):
        log.warning("Wiederherstellungscode benutzt für '%s' (%d übrig)", user["username"], len(rest))
        return True
    return False


async def login_2fa(request: Request):
    user = _pending_login_user(request)
    if not user:
        request.session.clear()
        flash(request, "Bitte erneut anmelden.", "warn")
        return redirect("/login")
    error, status = None, 200
    if request.method == "POST":
        ip = client_ip(request)
        form = await checked_form(request)
        if form is None:
            return forbidden()
        wait = auth.throttle.try_acquire(ip) or auth.throttle.try_acquire(f"user2fa:{user['id']}", global_=False)
        if wait:
            return render(request, "login_2fa.html", {"error": f"Zu viele Fehlversuche. Bitte {wait//60+1} Min. warten."}, 429)
        if _check_second_factor(user, form.get("code", "")):
            auth.throttle.success(ip)
            auth.throttle.success(f"user2fa:{user['id']}")
            long_ = bool(request.session.get("pre_long"))
            request.session.clear()
            request.session.update({"uid": user["id"], "ver": user["session_ver"], "since": int(time.time()),
                                    "long": long_})
            log.info("Login '%s' (2FA) von %s", user["username"], ip)
            notify.send("login", "🔐 Neue Anmeldung bei EarnWatch",
                        f"Benutzer {user['username']} (mit 2FA) · {notify.short_agent(request.headers.get('user-agent', ''))} · IP {ip}",
                        priority=3, tags=["lock"], dedupe=f"login:{ip}", dedupe_seconds=60)
            left = len(db.recovery_hashes(db.get_user(user["id"])))
            if left <= 2:
                flash(request, f"Nur noch {left} Wiederherstellungscode(s) übrig – in den Einstellungen neue erzeugen.", "warn")
            return redirect("/")
        log.warning("Falscher 2FA-Code von %s", ip)
        error, status = "Code ungültig oder schon benutzt.", 401
    return render(request, "login_2fa.html", {"error": error}, status)


async def twofa_setup(request: Request):
    user = current_user(request)
    if not user:
        return redirect("/login")
    if db.totp_secret(user):
        return redirect("/settings#2fa")
    secret = db.totp_pending(user)
    if not secret:
        secret = totp.new_secret()
        db.set_totp_pending(user["id"], secret)
    error = None
    if request.method == "POST":
        form = await checked_form(request)
        if form is None:
            return forbidden()
        if wait := auth.throttle.try_acquire(f"setup2fa:{user['id']}", global_=False):
            error = f"Zu viele Versuche. Bitte {wait//60+1} Min. warten."
        else:
            counter = totp.verify(secret, form.get("code", ""), 0)
            if counter is not None:
                codes = totp.new_recovery_codes()
                db.enable_totp(user["id"], secret, counter, [totp.hash_recovery(c) for c in codes])
                fresh = db.get_user(user["id"])
                request.session["ver"] = fresh["session_ver"]  # diese Sitzung bleibt, alle anderen fliegen raus
                auth.throttle.success(f"setup2fa:{user['id']}")
                log.info("2FA aktiviert für '%s'", user["username"])
                notify.send("security", "🛡️ 2FA eingeschaltet", f"Für {user['username']} ist die Zwei-Faktor-Anmeldung jetzt aktiv.", tags=["shield"])
                return render(request, "twofa_codes.html", {"user": fresh, "codes": codes, "fresh": True})
            error = "Der Code passt nicht. Uhrzeit am Handy prüfen und den aktuellen Code eingeben."
    svg = totp.qr_svg(totp.otpauth_uri(secret, user["username"]))
    return render(request, "twofa_setup.html", {
        "user": user, "secret": " ".join(secret[i:i + 4] for i in range(0, len(secret), 4)),
        "qr": Markup(svg) if svg else None, "error": error,
    })


async def twofa_manage(request: Request):
    user = current_user(request)
    if not user:
        return redirect("/login")
    form = await checked_form(request)
    if form is None:
        return forbidden()
    action = request.path_params["action"]
    if wait := auth.throttle.try_acquire(f"manage2fa:{user['id']}", global_=False):
        flash(request, f"Zu viele Versuche. Bitte {wait//60+1} Min. warten.", "err")
        return redirect("/settings#2fa")
    pw_ok = await run_in_threadpool(auth.verify_password, form.get("password", ""), user["pw_hash"])
    if not pw_ok or not _check_second_factor(user, form.get("code", "")):
        flash(request, "Passwort oder 2FA-Code falsch.", "err")
        return redirect("/settings#2fa")
    auth.throttle.success(f"manage2fa:{user['id']}")
    if action == "disable":
        db.disable_totp(user["id"])
        request.session["ver"] = db.get_user(user["id"])["session_ver"]
        log.warning("2FA deaktiviert für '%s'", user["username"])
        notify.send("security", "⚠️ 2FA ausgeschaltet", f"Für {user['username']} wurde die Zwei-Faktor-Anmeldung ausgeschaltet. Warst du das nicht? Sofort Passwort ändern!", priority=5, tags=["warning"])
        flash(request, "Zwei-Faktor-Anmeldung ausgeschaltet.", "warn")
        return redirect("/settings#2fa")
    if action == "recovery":
        codes = totp.new_recovery_codes()
        db.set_recovery(user["id"], [totp.hash_recovery(c) for c in codes])
        return render(request, "twofa_codes.html", {"user": user, "codes": codes, "fresh": False})
    return redirect("/settings#2fa")


async def logout(request: Request):
    form = await checked_form(request)
    if form is None:
        return forbidden()
    user = current_user(request)
    if user:
        db.bump_session(user["id"])  # alle Sitzungs-Cookies sofort ungültig (auch gestohlene)
    request.session.clear()
    return redirect("/login")


async def dashboard(request: Request):
    user = current_user(request)
    if not user:
        return redirect("/login" if db.user_count() else "/setup")
    view = request.query_params.get("view", "day")
    if view not in ("day", "week", "month", "year"):
        view = "day"
    period = request.query_params.get("period", "week")
    if period not in ("today", "week", "month", "year"):
        period = "week"
    data = stats.compute(view, period)
    now = dt.datetime.now(stats.TZ)
    hour = now.hour
    greeting = ("Gute Nacht" if hour < 5 else "Guten Morgen" if hour < 11 else
                "Guten Tag" if hour < 17 else "Guten Abend" if hour < 22 else "Gute Nacht")
    weekdays = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
    months = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August",
              "September", "Oktober", "November", "Dezember"]
    duino = next((p for p in data["platforms"] if p["id"] == "duinocoin"), None)
    try:
        achievements.evaluate(data)
    except Exception:
        log.exception("Fehler bei den Erfolgen")
    return render(request, "dashboard.html", {
        "ach": achievements.overview(data),
        "user": user,
        "d": data,
        "view": view,
        "period": period,
        "greeting": greeting,
        "today_str": f"{weekdays[now.weekday()]}, {now.day}. {months[now.month - 1]}",
        "bar_svg": charts.bar_chart(data["chart"]),
        "heat_svg": charts.heatmap(data["heat"]),
        "hero_svg": charts.hero_spark(data["hero_spark"]),
        "share_svg": charts.share_bar(data["share"]),
        "miners_svg": charts.miners(duino["info"].get("rigs", [])) if duino and duino["configured"] else None,
        "duino": duino,
        "sparks": {p["id"]: charts.spark(p["spark"], p["color"]) for p in data["platforms"]},
        "names": {k: v["name"] for k, v in PLATFORMS.items()},
        "units": {k: v["unit"] for k, v in PLATFORMS.items()},
    })


async def platform_page(request: Request):
    user = current_user(request)
    if not user:
        return redirect("/login" if db.user_count() else "/setup")
    pf = request.path_params["pf"]
    if pf not in PLATFORMS:   # nur bekannte Plattformen, alles andere -> 404
        return PlainTextResponse("Nicht gefunden", status_code=404)
    view = request.query_params.get("view", "day")
    if view not in ("day", "week", "month", "year"):
        view = "day"
    period = request.query_params.get("period", "week")
    if period not in ("today", "week", "month", "year"):
        period = "week"
    det = stats.platform_detail(pf, view, period)
    p = det["p"]
    return render(request, "platform.html", {
        "user": user, "x": det, "p": p, "view": view, "period": period,
        "bars_svg": charts.platform_bars(det["bars"], p["color"], p["unit"], det["avg"]),
        "area_svg": charts.balance_area(det["balance"], p["color"], p["unit"]),
        "wd_svg": charts.weekday_bars(det["weekday"], p["color"], p["unit"]),
        "heat_svg": charts.heatmap(det["heat"], p["unit"]),
        "miners_svg": charts.miners(p["info"].get("rigs", [])) if pf == "duinocoin" and p["configured"] else None,
        "others": [(k, v["name"], v["short"]) for k, v in PLATFORMS.items()],
    })


_bg_tasks: set = set()


async def _manual_collect():
    """Läuft im Hintergrund – die Seite muss nicht 30 Sekunden auf alle Anbieter warten."""
    try:
        results = await collect_all()
        if not results:
            msg, kind = "Noch keine Zugangsdaten hinterlegt – siehe Einstellungen.", "warn"
        elif bad := [r for r in results if not r.ok]:
            msg, kind = " · ".join(f"{PLATFORMS[r.platform]['name']}: {r.message}" for r in bad), "err"
        else:
            msg, kind = "Kontostände aktualisiert.", "ok"
    except Exception:
        log.exception("Fehler beim manuellen Abruf")
        msg, kind = "Abruf fehlgeschlagen – Details im Container-Log.", "err"
    db.set_setting("manual_result", json.dumps({"ts": time.time(), "msg": msg[:400], "kind": kind}))


def _back_target(form) -> str:
    """Nur feste, eigene Ziele erlauben (keine offene Weiterleitung)."""
    back = form.get("back") or "/"
    if back.startswith("/p/") and back[3:] in PLATFORMS:
        return back
    return "/"


# --------------------------------------------------------------------------
# Passkeys (Face ID / Touch ID)
# --------------------------------------------------------------------------
def _rp(request: Request) -> tuple[str, str] | None:
    """RP-ID (Domain) und erwartete Herkunft. Passkeys gehen nur mit einem Namen, nicht mit einer IP."""
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    if not name or name.replace(".", "").isdigit() or ":" in name or "[" in name:
        return None
    if not all(c.isalnum() or c in ".-" for c in name) or len(name) > 253:
        return None
    scheme = "http" if name == "localhost" else "https"   # Browser erlauben WebAuthn nur über HTTPS (oder localhost)
    return name.lower(), f"{scheme}://{host.lower()}"


async def _checked_json(request: Request) -> dict | None:
    """JSON-Anfrage: Content-Type, Herkunft und CSRF-Token (im Header) prüfen."""
    if not request.headers.get("content-type", "").startswith("application/json"):
        return None
    origin = request.headers.get("origin")
    hosts = {request.headers.get("host"), request.headers.get("x-forwarded-host")}
    if not origin or urlparse(origin).netloc not in hosts:
        return None
    if not auth.csrf_ok(request.session, request.headers.get("x-csrf-token")):
        return None
    try:
        data = json.loads(await request.body())
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _jerr(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg}, status_code=status)


async def passkey_api(request: Request):
    step = request.path_params["step"]
    data = await _checked_json(request)
    if data is None:
        return _jerr("Ungültige Anfrage – Seite neu laden.", 403)
    rp = _rp(request)
    if rp is None:
        return _jerr("Passkeys funktionieren nur über deine Tailscale-Adresse (…ts.net), nicht über die IP.")
    rp_id, origin = rp
    ip = client_ip(request)

    if step == "register-options":
        user = current_user(request)
        if not user:
            return _jerr("Bitte anmelden.", 401)
        if wait := auth.throttle.try_acquire(f"pwchange:{user['id']}", global_=False):
            return _jerr(f"Zu viele Versuche. Bitte {wait // 60 + 1} Min. warten.", 429)
        if not await run_in_threadpool(auth.verify_password, str(data.get("password", ""))[:200], user["pw_hash"]):
            return _jerr("Passwort ist falsch.", 401)
        auth.throttle.success(f"pwchange:{user['id']}")
        ch = webauthn.new_challenge()
        request.session["pk_reg"] = {"c": ch, "t": int(time.time()), "rp": rp_id}
        return JSONResponse({"ok": True, "options": webauthn.registration_options(
            user["id"], user["username"], rp_id, ch, [p["cred_id"] for p in db.passkeys(user["id"])])})

    if step == "register-finish":
        user = current_user(request)
        st = request.session.pop("pk_reg", None)
        if not user or not st or time.time() - st["t"] > 300 or st["rp"] != rp_id:
            return _jerr("Zeit abgelaufen – bitte nochmal.", 400)
        try:
            res = webauthn.verify_registration(data.get("response") or {}, st["c"], origin, rp_id)
        except (webauthn.WebAuthnError, ValueError, TypeError, KeyError, AttributeError) as e:
            log.warning("Passkey-Registrierung abgelehnt: %s", e)
            return _jerr(f"Passkey abgelehnt: {e}")
        if db.passkey_by_cred(res["cred_id"]):
            return _jerr("Dieser Passkey ist schon gespeichert.")
        name = "".join(ch for ch in str(data.get("name") or notify.short_agent(request.headers.get("user-agent", "")))
                       if ch.isprintable())[:60] or "Passkey"
        db.add_passkey(user["id"], res["cred_id"], res["public_key"], res["alg"], res["sign_count"], rp_id, name)
        notify.send("security", "🔑 Neuer Passkey hinzugefügt", f"„{name}“ kann sich jetzt per Face ID bei EarnWatch anmelden.",
                    priority=4, tags=["key"])
        flash(request, f"Passkey „{name}“ gespeichert – ab jetzt geht der Login per Face ID.")
        return JSONResponse({"ok": True})

    if step == "login-options":
        if wait := auth.throttle.blocked(ip):
            return _jerr(f"Zu viele Versuche. Bitte {wait // 60 + 1} Min. warten.", 429)
        ch = webauthn.new_challenge()
        request.session["pk_auth"] = {"c": ch, "t": int(time.time()), "rp": rp_id}
        return JSONResponse({"ok": True, "options": {"challenge": ch, "rpId": rp_id, "timeout": 120000,
                                                    "userVerification": "required", "allowCredentials": []}})

    if step == "login-finish":
        st = request.session.pop("pk_auth", None)
        if not st or time.time() - st["t"] > 300 or st["rp"] != rp_id:
            return _jerr("Zeit abgelaufen – bitte nochmal.")
        if wait := auth.throttle.try_acquire(ip):
            return _jerr(f"Zu viele Versuche. Bitte {wait // 60 + 1} Min. warten.", 429)
        stored = db.passkey_by_cred(str(data.get("id", ""))[:1500])
        user = db.get_user(stored["user_id"]) if stored else None
        try:
            if not stored or not user or stored["rp_id"] != rp_id:
                raise webauthn.WebAuthnError("Unbekannter Passkey")
            count = webauthn.verify_assertion(data.get("response") or {}, stored, st["c"], origin)
            if not db.passkey_used(stored["id"], stored["sign_count"], count):
                raise webauthn.WebAuthnError("Passkey gleichzeitig benutzt")
        except (webauthn.WebAuthnError, ValueError, TypeError, KeyError, AttributeError) as e:
            log.warning("Passkey-Login abgelehnt von %s: %s", ip, e)
            return _jerr("Passkey-Anmeldung fehlgeschlagen.", 401)
        auth.throttle.success(ip)
        request.session.clear()
        request.session.update({"uid": user["id"], "ver": user["session_ver"], "since": int(time.time()), "long": True})
        log.info("Login '%s' per Passkey von %s", user["username"], ip)
        notify.send("login", "🔐 Neue Anmeldung bei EarnWatch",
                    f"Benutzer {user['username']} (Passkey „{stored['name']}“) · {notify.short_agent(request.headers.get('user-agent', ''))} · IP {ip}",
                    priority=3, tags=["lock"], dedupe=f"login:{ip}", dedupe_seconds=60)
        return JSONResponse({"ok": True, "next": "/"})

    return _jerr("Unbekannt", 404)


async def achievements_page(request: Request):
    user = current_user(request)
    if not user:
        return redirect("/login" if db.user_count() else "/setup")
    return render(request, "achievements.html", {"user": user, "ach": achievements.overview(stats.compute())})


async def collect_now(request: Request):
    global _last_manual
    if not current_user(request):
        return redirect("/login")
    form = await checked_form(request)
    if form is None:
        return forbidden()
    target = _back_target(form)
    if time.time() - _last_manual < 60:
        flash(request, "Bitte höchstens einmal pro Minute manuell abrufen.", "warn")
        return redirect(target)
    _last_manual = time.time()
    request.session["await_collect"] = int(time.time())
    task = asyncio.create_task(_manual_collect())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return redirect(target)


async def settings_page(request: Request):
    user = current_user(request)
    if not user:
        return redirect("/login")
    conf = db.configured_platforms()
    return render(request, "settings.html", {
        "user": user,
        "twofa": bool(db.totp_secret(user)),
        "ncfg": notify.config(),
        "nevents": notify.EVENTS,
        "topic_suggestion": notify.random_topic(),
        "recovery_left": len(db.recovery_hashes(user)),
        "platforms": PLATFORMS,
        "configured": conf,
        "runs": db.last_runs(),
        "containers": _json_setting("containers"),
        "exp": {pf: int(db.get_setting(f"cred_exp_{pf}", "") or 0) for pf in conf},
        "now": time.time(),
        "interval": db.get_setting("interval_min", str(config.INTERVAL_MIN_DEFAULT)),
        "passkeys": db.passkeys(user["id"]),
        "widget_ts": int(db.get_setting("widget_token_ts", "0") or 0) if db.get_setting("widget_token_hash", "") else 0,
        "currency": db.get_setting("currency", "USD"),
        "theme": db.get_setting("theme", "auto"),
        "eur_rate": float(db.get_setting("eur_rate", "0") or 0),
        "eur_rate_ts": int(db.get_setting("eur_rate_ts", "0") or 0),
    })


async def settings_save(request: Request):
    user = current_user(request)
    if not user:
        return redirect("/login")
    form = await checked_form(request)
    if form is None:
        return forbidden()
    what = request.path_params["what"]
    if what == "notify":
        cfg, err = notify.validate(form)
        if err:
            flash(request, err, "err")
        else:
            db.save_credentials("_notify", cfg)
            evs = [e for e in notify.EVENTS if form.get("ev_" + e) == "1"]
            db.set_setting("notify_events", json.dumps(evs))
            try:
                h = int(form.get("summary_hour", "21"))
                db.set_setting("notify_summary_hour", str(h if 0 <= h <= 23 else 21))
            except ValueError:
                pass
            flash(request, "Benachrichtigungen gespeichert." + ("" if notify.enabled() else " (Noch kein Dienst eingetragen.)"))
        return redirect("/settings#notify")
    if what == "notifytest":
        if not notify.enabled():
            flash(request, "Erst ntfy oder Telegram eintragen und speichern.", "warn")
            return redirect("/settings#notify")
        if wait := auth.throttle.try_acquire(f"notifytest:{user['id']}", global_=False):
            flash(request, f"Bitte {wait//60+1} Min. warten.", "warn")
            return redirect("/settings#notify")
        errs = await run_in_threadpool(lambda: notify.send("test", "✅ EarnWatch-Test",
                                       "Benachrichtigungen funktionieren!", tags=["tada"], wait=True))
        flash(request, ("Fehler: " + "; ".join(errs)) if errs else "Test gesendet – schau aufs Handy.", "err" if errs else "ok")
        return redirect("/settings#notify")
    if what in PLATFORMS and PLATFORMS[what].get("manual"):
        meta = PLATFORMS[what]
        try:
            val = float((form.get("balance") or "").replace("$", "").replace(",", ".").strip())
            if not 0 <= val < 1_000_000:
                raise ValueError
            db.add_snapshot(what, round(val, 4), meta["unit"])
            db.add_run(what, True, None)
            flash(request, f"{meta['name']}: Kontostand {charts.money(val)} eingetragen.")
        except ValueError:
            flash(request, "Bitte einen Betrag wie 3,47 eingeben.", "err")
        if form.get("back") == "/":
            return redirect("/")
        if form.get("back") == "detail":   # feste Ziele, keine frei wählbare Weiterleitung
            return redirect(f"/p/{what}")
    elif what in PLATFORMS:
        meta = PLATFORMS[what]
        data, missing = {}, []
        for f in meta["fields"]:
            v = (form.get(f["key"]) or "").strip()
            if f["required"] and not v:
                missing.append(f["label"])
            data[f["key"]] = v
        if missing:
            flash(request, "Bitte ausfüllen: " + ", ".join(missing), "err")
        elif what == "honeygain" and "@" not in data["email"]:
            flash(request, "Bitte eine gültige E-Mail eingeben.", "err")
        elif what == "honeygain_jt" and any(data.values()) and not all(data.values()):
            flash(request, "Bitte E-Mail UND Passwort eintragen – oder beides leer lassen.", "err")
        elif not any(data.values()):
            db.delete_credentials(what)
            flash(request, f"{meta['name']}: nutzt jetzt den Zugang von {PLATFORMS[meta['fallback_creds']]['name']}."
                  if meta.get("fallback_creds") else f"{meta['name']}: nichts eingetragen.")
        else:
            db.save_credentials(what, data)
            flash(request, f"{meta['name']}: Zugang verschlüsselt gespeichert.")
    elif what == "delete":
        pf = form.get("platform", "")
        if pf in PLATFORMS:
            db.delete_credentials(pf)
            flash(request, f"Zugangsdaten für {PLATFORMS[pf]['name']} gelöscht.")
    elif what == "passkey_delete":
        try:
            pk = int(form.get("id", "0"))
        except ValueError:
            pk = 0
        if db.delete_passkey(user["id"], pk):
            notify.send("security", "🔑 Passkey entfernt", "Ein Passkey wurde aus EarnWatch gelöscht.", priority=3, tags=["key"])
            flash(request, "Passkey gelöscht.")
        return redirect("/settings#passkeys")
    elif what == "widget":
        if form.get("action") == "delete":
            db.set_setting("widget_token_hash", "")
            flash(request, "Widget-Schlüssel gelöscht – das Widget zeigt jetzt nichts mehr an.")
        else:
            token = "ew_" + secrets.token_urlsafe(32)
            db.set_setting("widget_token_hash", hashlib.sha256(token.encode()).hexdigest())
            db.set_setting("widget_token_ts", str(int(time.time())))
            notify.send("security", "📱 Widget-Schlüssel erstellt",
                        f"Für EarnWatch wurde ein neuer Widget-Schlüssel erzeugt ({user['username']}).", priority=3, tags=["iphone"])
            host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
            return render(request, "widget.html", {"user": user, "token": token,
                                                    "api_url": f"https://{host}/api/widget"})
        return redirect("/settings#widget")
    elif what == "general":
        try:
            interval = int(form.get("interval", "60"))
            if not 15 <= interval <= 1440:
                raise ValueError
            db.set_setting("interval_min", str(interval))
            cur = form.get("currency", "USD")
            if cur not in ("USD", "EUR"):
                raise ValueError
            theme = form.get("theme", "auto")
            if theme not in ("auto", "dark", "light"):
                raise ValueError
            style = form.get("style", "glass")
            if style not in ("glass", "nothing"):
                raise ValueError
            db.set_setting("theme", theme)
            db.set_setting("ui_style", style)
            db.set_setting("currency", cur)
            msg = "Einstellungen gespeichert."
            if cur == "EUR":
                rate = await run_in_threadpool(fx.update_now)
                if rate:
                    msg += f" Aktueller EZB-Kurs: 1 $ = {rate:.4f} €".replace(".", ",")
                elif float(db.get_setting("eur_rate", "0") or 0) <= 0:
                    msg = "Euro gespeichert, aber der EZB-Kurs ist gerade nicht erreichbar – bis dahin wird in $ angezeigt."
            charts.reset_currency_cache()
            flash(request, msg)
        except ValueError:
            flash(request, "Intervall 15–1440 Minuten.", "err")
    elif what == "password":
        old, new, new2 = form.get("old", ""), form.get("new", ""), form.get("new2", "")
        if wait := auth.throttle.try_acquire(f"pwchange:{user['id']}", global_=False):
            flash(request, f"Zu viele Versuche. Bitte {wait//60+1} Min. warten.", "err")
        elif not await run_in_threadpool(auth.verify_password, old, user["pw_hash"]):
            flash(request, "Altes Passwort ist falsch.", "err")
        elif new != new2:
            flash(request, "Die neuen Passwörter stimmen nicht überein.", "err")
        elif problem := auth.password_problems(new):
            flash(request, problem, "err")
        else:
            db.set_password(user["id"], await run_in_threadpool(auth.hash_password, new))
            notify.send("security", "🔑 Passwort geändert", f"Das EarnWatch-Passwort von {user['username']} wurde geändert. Alle Sitzungen wurden beendet.", priority=4, tags=["key"])
            request.session.clear()
            flash(request, "Passwort geändert – bitte neu anmelden. Alle anderen Sitzungen wurden beendet.")
            return redirect("/login")
    return redirect("/settings")


async def export_csv(request: Request):
    if not current_user(request):
        return redirect("/login")
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["plattform", "zeitpunkt", "kontostand", "waehrung"])
    for r in db.snapshots():
        w.writerow([r["platform"], dt.datetime.fromtimestamp(r["ts"], stats.TZ).isoformat(), f"{r['balance']:.4f}", r["currency"]])
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"content-disposition": 'attachment; filename="earnwatch.csv"'})


async def live_css(request: Request):
    """Live-Geldzähler ohne JavaScript: Startwert + Tempo als CSS-Animation (zählt eine Stunde lang hoch)."""
    css = "/* nicht angemeldet */"
    if current_user(request):
        d = stats.compute("day", "week")
        code, rate = charts.currency()
        bal = d["totals"]["balance"] * rate
        per_sec = max(0.0, d["totals"]["d7_usd"] / 7 / 86400 * rate) if "d7_usd" in d["totals"] else 0.0
        dec = 5 if bal < 20000 else 3
        scale = 10 ** dec
        n0 = int(bal * scale)
        n1 = int((bal + per_sec * 3600) * scale)
        if per_sec > 0 and n1 > n0 and n1 < 2_000_000_000:
            sym_pre, sep, sym_post = ("$", ".", "") if code == "USD" else ("", ",", " €")
            # Nachkommastellen in Ziffern-Gruppen zerlegen (CSS-Zähler können nur 2 Stellen mit Null auffüllen)
            groups, rest = [], dec
            while rest > 0:
                g = 2 if rest >= 2 else 1
                groups.append(g)
                rest -= g
            props, decls, parts = ["la"], [f"--la:calc((var(--lv) - mod(var(--lv),{scale})) / {scale})"], []
            used = 0
            for k, g in enumerate(groups):
                lo = 10 ** (dec - used - g)
                hi = 10 ** (dec - used)
                name = f"l{k}"
                props.append(name)
                decls.append(f"--{name}:calc((mod(var(--lv),{hi}) - mod(var(--lv),{lo})) / {lo})")
                parts.append(f"counter({name},decimal-leading-zero)" if g == 2 else f"counter({name})")
                used += g
            regs = "\n".join(f"@property --{n}{{{{syntax:'<integer>';inherits:false;initial-value:0}}}}" for n in ["lv"] + props)
            css = (regs.replace("{{", "{").replace("}}", "}") + f"""
@keyframes lvc{{from{{--lv:{n0}}}to{{--lv:{n1}}}}}
@supports (top:mod(3px,2px)){{
@media (prefers-reduced-motion:no-preference){{
.live .st{{display:none}}
.live .lv{{display:inline;--lv:{n0};animation:lvc 3600s linear forwards;{";".join(decls)};
  counter-reset:{" ".join(f"{n} var(--{n})" for n in props)}}}
.live .lv::after{{content:"{sym_pre}" counter(la) "{sep}" {" ".join(parts)} "{sym_post}"}}
.livetag{{display:inline-flex}}
}}}}""")
    return Response(css, media_type="text/css; charset=utf-8")


_api_throttle = auth.LoginThrottle(per_ip=10, global_max=200, window=900)   # eigene Bremse, sperrt nie den Login


async def api_widget(request: Request):
    """Nur-Lese-Schnittstelle fürs iPhone-Widget. Schlüssel im Header, nur zusammengefasste Zahlen."""
    ip = client_ip(request)
    key = f"api:{ip}"
    if wait := _api_throttle.blocked(key):
        return JSONResponse({"error": "zu viele Versuche"}, status_code=429, headers={"retry-after": str(wait)})
    stored = db.get_setting("widget_token_hash", "")
    hdr = request.headers.get("authorization", "")
    tok = hdr[7:].strip() if hdr.lower().startswith("bearer ") else ""
    if not stored or not tok or len(tok) > 200 or not hmac.compare_digest(
            hashlib.sha256(tok.encode()).hexdigest(), stored):
        _api_throttle.fail(key)
        return JSONResponse({"error": "ungültiger Schlüssel"}, status_code=401)
    d = stats.compute("day", "today")
    t = d["totals"]
    code, _ = charts.currency()
    top = sorted((p for p in d["platforms"] if p["configured"] and p["fac"]), key=lambda p: -p["today"] * p["fac"])[:4]
    return JSONResponse({
        "currency": code,
        "balance": round(t["balance"], 4), "balance_fmt": charts.money(t["balance"]),
        "today_fmt": charts.money(t["today"]), "week_fmt": charts.money(t["week"]),
        "month_fmt": charts.money(t["month"]),
        "devices_online": d["devices"]["online"], "devices_total": d["devices"]["total"],
        "streak": d["streaks"]["current"],
        "top": [{"name": p["name"], "today_fmt": charts.money(p["today"] * p["fac"])} for p in top],
        "errors": sum(1 for p in d["platforms"] if p["configured"] and p["run"] and not p["run"]["ok"]),
        "updated": int(db.get_setting("last_collect_ts", "0") or 0),
    })


async def healthz(request: Request):
    return PlainTextResponse("ok")


@contextlib.asynccontextmanager
async def lifespan(app):
    db.init()
    if db.user_count() == 0:
        if config._read_secret("EW_SETUP_TOKEN"):
            log.warning("Noch kein Konto angelegt. Einrichtungs-Token = EW_SETUP_TOKEN aus deiner .env")
        else:
            log.warning("Noch kein Konto angelegt. Einrichtungs-Token: %s", config.setup_token())
    try:
        events.scan_expiry()
    except Exception:
        log.exception("Ablauf-Scan fehlgeschlagen")
    task = asyncio.create_task(scheduler())
    task2 = asyncio.create_task(events.summary_loop())
    task3 = asyncio.create_task(fx.fx_loop())
    yield
    task.cancel()
    task2.cancel()
    task3.cancel()


routes = [
    Route("/", dashboard),
    Route("/setup", setup, methods=["GET", "POST"]),
    Route("/login", login, methods=["GET", "POST"]),
    Route("/logout", logout, methods=["POST"]),
    Route("/login/2fa", login_2fa, methods=["GET", "POST"]),
    Route("/2fa/setup", twofa_setup, methods=["GET", "POST"]),
    Route("/2fa/{action:str}", twofa_manage, methods=["POST"]),
    Route("/p/{pf:str}", platform_page),
    Route("/erfolge", achievements_page),
    Route("/passkey/{step:str}", passkey_api, methods=["POST"]),
    Route("/collect", collect_now, methods=["POST"]),
    Route("/settings", settings_page),
    Route("/settings/{what:str}", settings_save, methods=["POST"]),
    Route("/export.csv", export_csv),
    Route("/healthz", healthz),
    Route("/live.css", live_css),
    Route("/api/widget", api_widget),
    Mount("/static", StaticFiles(directory=str(BASE / "static")), name="static"),
]

app = Starlette(
    routes=routes,
    lifespan=lifespan,
    middleware=[
        Middleware(SecurityHeaders),
        Middleware(
            SessionMiddleware,
            secret_key=config.session_secret(),
            session_cookie="ew_session",
            max_age=max(config.SESSION_HOURS * 3600, config.REMEMBER_DAYS * 86400),
            # "lax": iPhone-Web-Apps (Home-Bildschirm) schicken "strict"-Cookies nicht zuverlässig mit.
            # Schutz vor fremden Formularen bleibt: CSRF-Token + Origin-Prüfung + Lax blockt fremde POSTs.
            same_site="lax",
            https_only=config.SECURE_COOKIES,
        ),
    ],
)
