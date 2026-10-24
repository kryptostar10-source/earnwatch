"""EarnWatch – sicheres Einnahmen-Dashboard für Honeygain & EarnApp."""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import csv
import datetime as dt
import io
import json
import logging
import math
import time
from pathlib import Path
from urllib.parse import urlparse

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from markupsafe import Markup

from . import auth, charts, config, db, stats, totp
from .collectors import PLATFORMS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("earnwatch")

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
templates.env.globals["money"] = charts.money
templates.env.globals["amount"] = charts.amount


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
    "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)


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
                for k, v in (
                    ("content-security-policy", CSP),
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
    if time.time() - request.session.get("since", 0) > config.SESSION_HOURS * 3600:
        request.session.clear()
        return None
    return user


def render(request: Request, name: str, ctx: dict | None = None, status: int = 200):
    ctx = dict(ctx or {})
    ctx["csrf"] = auth.csrf_token(request.session)
    ctx.setdefault("user", None)
    ctx["flash"] = request.session.pop("flash", None)
    if ctx["user"]:
        interval = max(15, int(db.get_setting("interval_min", str(config.INTERVAL_MIN_DEFAULT))))
        last = int(db.get_setting("last_collect_ts", "0") or 0)
        nxt = max(0, int((last + interval * 60 - time.time()) / 60)) if last else None
        ctx["status"] = {"interval": interval, "next_in": nxt,
                         "frac": round(1 - nxt / interval, 3) if nxt is not None else 0}
    return templates.TemplateResponse(request, name, ctx, status_code=status)


def flash(request: Request, msg: str, kind: str = "ok"):
    request.session["flash"] = {"msg": msg, "kind": kind}


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


async def checked_form(request: Request):
    """Formular lesen + CSRF-Token und Origin prüfen. Gibt None bei Angriff zurück."""
    origin = request.headers.get("origin")
    if origin and origin != "null":
        if urlparse(origin).netloc != request.headers.get("host"):
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
        if wait := auth.throttle.try_acquire(ip):
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
        if wait := auth.throttle.try_acquire(ip):
            return render(request, "login.html", {"error": f"Zu viele Fehlversuche. Bitte {wait//60+1} Min. warten."}, 429)
        user = db.get_user_by_name(form.get("username", "").strip())
        pw = form.get("password", "")
        stored = user["pw_hash"] if user else auth.DUMMY_HASH
        ok = await run_in_threadpool(auth.verify_password, pw, stored)
        if user and ok:
            request.session.clear()  # neue Session (gegen Session-Fixation)
            if db.totp_secret(user):
                # Passwort stimmt – jetzt noch der zweite Faktor (max. 5 Minuten Zeit)
                request.session.update({"pre_uid": user["id"], "pre_ver": user["session_ver"],
                                        "pre_ts": int(time.time())})
                return redirect("/login/2fa")
            auth.throttle.success(ip)
            request.session.update({"uid": user["id"], "ver": user["session_ver"], "since": int(time.time())})
            log.info("Login '%s' von %s", user["username"], ip)
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
            request.session.clear()
            request.session.update({"uid": user["id"], "ver": user["session_ver"], "since": int(time.time())})
            log.info("Login '%s' (2FA) von %s", user["username"], ip)
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
    return render(request, "dashboard.html", {
        "user": user,
        "d": data,
        "view": view,
        "period": period,
        "greeting": greeting,
        "today_str": f"{weekdays[now.weekday()]}, {now.day}. {months[now.month - 1]}",
        "bar_svg": charts.bar_chart(data["chart"]),
        "hero_svg": charts.hero_spark(data["hero_spark"]),
        "share_svg": charts.share_bar(data["share"]),
        "miners_svg": charts.miners(duino["info"].get("rigs", [])) if duino and duino["configured"] else None,
        "duino": duino,
        "sparks": {p["id"]: charts.spark(p["spark"], p["color"]) for p in data["platforms"]},
        "eur_rate": float(db.get_setting("eur_rate", "0") or 0),
        "names": {k: v["name"] for k, v in PLATFORMS.items()},
        "units": {k: v["unit"] for k, v in PLATFORMS.items()},
    })


async def collect_now(request: Request):
    global _last_manual
    if not current_user(request):
        return redirect("/login")
    if await checked_form(request) is None:
        return forbidden()
    if time.time() - _last_manual < 60:
        flash(request, "Bitte höchstens einmal pro Minute manuell abrufen.", "warn")
        return redirect("/")
    _last_manual = time.time()
    results = await collect_all()
    if not results:
        flash(request, "Noch keine Zugangsdaten hinterlegt – siehe Einstellungen.", "warn")
    else:
        bad = [r for r in results if not r.ok]
        if bad:
            flash(request, " · ".join(f"{PLATFORMS[r.platform]['name']}: {r.message}" for r in bad), "err")
        else:
            flash(request, "Kontostände aktualisiert.")
    return redirect("/")


async def settings_page(request: Request):
    user = current_user(request)
    if not user:
        return redirect("/login")
    conf = db.configured_platforms()
    return render(request, "settings.html", {
        "user": user,
        "twofa": bool(db.totp_secret(user)),
        "recovery_left": len(db.recovery_hashes(user)),
        "platforms": PLATFORMS,
        "configured": conf,
        "runs": db.last_runs(),
        "interval": db.get_setting("interval_min", str(config.INTERVAL_MIN_DEFAULT)),
        "eur_rate": db.get_setting("eur_rate", ""),
    })


async def settings_save(request: Request):
    user = current_user(request)
    if not user:
        return redirect("/login")
    form = await checked_form(request)
    if form is None:
        return forbidden()
    what = request.path_params["what"]
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
    elif what == "general":
        try:
            interval = int(form.get("interval", "60"))
            if not 15 <= interval <= 1440:
                raise ValueError
            db.set_setting("interval_min", str(interval))
            rate = form.get("eur_rate", "").replace(",", ".").strip()
            if rate:
                r = float(rate)
                if not 0 < r < 10:
                    raise ValueError
                db.set_setting("eur_rate", str(r))
            else:
                db.set_setting("eur_rate", "0")
            flash(request, "Einstellungen gespeichert.")
        except ValueError:
            flash(request, "Intervall 15–1440 Minuten, Kurs z. B. 0,92.", "err")
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
    task = asyncio.create_task(scheduler())
    yield
    task.cancel()


routes = [
    Route("/", dashboard),
    Route("/setup", setup, methods=["GET", "POST"]),
    Route("/login", login, methods=["GET", "POST"]),
    Route("/logout", logout, methods=["POST"]),
    Route("/login/2fa", login_2fa, methods=["GET", "POST"]),
    Route("/2fa/setup", twofa_setup, methods=["GET", "POST"]),
    Route("/2fa/{action:str}", twofa_manage, methods=["POST"]),
    Route("/collect", collect_now, methods=["POST"]),
    Route("/settings", settings_page),
    Route("/settings/{what:str}", settings_save, methods=["POST"]),
    Route("/export.csv", export_csv),
    Route("/healthz", healthz),
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
            max_age=config.SESSION_HOURS * 3600,
            same_site="strict",
            https_only=config.SECURE_COOKIES,
        ),
    ],
)
