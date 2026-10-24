"""Erkennt wichtige Ereignisse beim Abruf und meldet sie über notify."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging

from . import charts, db, notify, stats

log = logging.getLogger("earnwatch.events")


def after_result(res, meta: dict, prev_run) -> None:
    pf = res.platform
    name = meta["name"]
    try:
        # --- Abruf fehlgeschlagen / wieder ok ---------------------------------
        if not res.ok:
            if prev_run is None or prev_run["ok"]:
                notify.send("error", f"⚠️ {name}: Abruf fehlgeschlagen", res.message or "Unbekannter Fehler",
                            priority=4, tags=["warning"], dedupe=f"err:{pf}", dedupe_seconds=6 * 3600)
            return
        if prev_run is not None and not prev_run["ok"]:
            notify.send("error", f"✅ {name} läuft wieder", "Der Abruf funktioniert wieder.", priority=2,
                        tags=["white_check_mark"], dedupe=f"ok:{pf}", dedupe_seconds=3600)

        # --- Auszahlungsgrenze erreicht ---------------------------------------
        mp = meta.get("min_payout")
        if mp and not meta.get("lifetime") and (res.info or {}).get("modus") != "JumpTask":
            flag = db.get_setting(f"payout_flag_{pf}", "0")
            if res.balance >= mp and flag != "1":
                notify.send("payout", f"💰 {name}: Auszahlung möglich",
                            f"Guthaben {charts.money(res.balance)} – die Grenze von {charts.money(mp)} ist erreicht.",
                            priority=4, tags=["moneybag"])
                db.set_setting(f"payout_flag_{pf}", "1")
            elif res.balance < mp * 0.5 and flag == "1":
                db.set_setting(f"payout_flag_{pf}", "0")   # nach der Auszahlung wieder scharf schalten

        # --- Geräte offline (Honeygain, EarnApp, Pawns, Grass …) -------------
        info = res.info or {}
        if pf != "duinocoin" and isinstance(info.get("online"), int):
            on, total = info["online"], int(info.get("geraete") or 0)
            try:
                before = int(db.get_setting(f"dev_online_{pf}", "-1"))
            except ValueError:
                before = -1
            if before >= 0 and on < before:
                notify.send("miner", f"📴 {name}: {before - on} Gerät{'e' if before - on != 1 else ''} offline",
                            f"Jetzt online: {on} von {total}", priority=4, tags=["electric_plug"],
                            dedupe=f"dev:{pf}:{on}", dedupe_seconds=3 * 3600)
            elif before >= 0 and on > before and before < total:
                notify.send("miner", f"✅ {name}: Gerät wieder online", f"Jetzt online: {on} von {total}",
                            priority=2, tags=["electric_plug"])
            db.set_setting(f"dev_online_{pf}", str(on))

        # --- Duino-Miner offline ----------------------------------------------
        if pf == "duinocoin":
            rigs = [r.get("name", "?") for r in (res.info or {}).get("rigs", [])]
            try:
                before = json.loads(db.get_setting("duino_rigs", "[]"))
            except ValueError:
                before = []
            gone = sorted(set(before) - set(rigs))
            back = sorted(set(rigs) - set(before)) if before else []
            if gone:
                notify.send("miner", f"⛏️ Duino: {len(gone)} Miner offline",
                            f"Offline: {', '.join(gone[:10])}\nOnline: {len(rigs)} von {len(set(before) | set(rigs))}",
                            priority=4, tags=["pick"])
            elif back and len(rigs) > len(before):
                notify.send("miner", "⛏️ Duino: Miner wieder online", f"Wieder da: {', '.join(back[:10])}",
                            priority=2, tags=["pick"])
            db.set_setting("duino_rigs", json.dumps(rigs[:50]))
    except Exception:  # Benachrichtigungen dürfen den Abruf nie stören
        log.exception("Fehler beim Auswerten der Ereignisse")


def summary_text() -> tuple[str, str]:
    d = stats.compute("day", "today")
    t = d["totals"]
    lines = [f"Heute: {charts.money(t['today'])}", f"Diese Woche: {charts.money(t['week'])}",
             f"Dieser Monat: {charts.money(t['month'])}", f"Guthaben gesamt: {charts.money(t['balance'])}"]
    top = sorted((p for p in d["platforms"] if p["configured"] and p["fac"]), key=lambda p: -p["today"] * p["fac"])[:3]
    if top and top[0]["today"] > 0:
        lines.append("Top heute: " + ", ".join(f"{p['name']} {charts.money(p['today'] * p['fac'])}" for p in top if p["today"] > 0))
    bad = [p["name"] for p in d["platforms"] if p["configured"] and p["run"] and not p["run"]["ok"]]
    if bad:
        lines.append("⚠️ Fehler bei: " + ", ".join(bad))
    return f"📊 EarnWatch: {charts.money(t['today'])} heute", "\n".join(lines)


def check_expiry() -> None:
    """Warnt 3 Tage vorher, wenn ein kopierter Login-Token (z. B. Grass) abläuft – einmal pro Token."""
    import time
    from .collectors import PLATFORMS
    for pf in db.configured_platforms():
        try:
            exp = int(db.get_setting(f"cred_exp_{pf}", "") or 0)
        except ValueError:
            continue
        if not exp:
            continue
        left = (exp - time.time()) / 86400
        if left <= 3 and db.get_setting(f"exp_notified_{pf}", "") != str(exp):
            name = PLATFORMS.get(pf, {}).get("name", pf)
            when = "ist abgelaufen" if left <= 0 else f"läuft in {max(1, round(left))} Tag{'en' if round(left) != 1 else ''} ab"
            notify.send("expiry", f"⏳ {name}: Login {when}",
                        "Bitte in EarnWatch → Einstellungen den Token/Cookie neu eintragen, sonst stoppt die Abfrage.",
                        priority=4, tags=["hourglass"])
            db.set_setting(f"exp_notified_{pf}", str(exp))


def scan_expiry() -> None:
    """Beim Start einmal alle gespeicherten Zugänge auf Ablaufdatum prüfen (für ältere Einträge)."""
    for pf in db.configured_platforms():
        try:
            db.remember_expiry(pf, db.load_credentials(pf) or {})
        except Exception:
            log.warning("Ablaufdatum für %s nicht lesbar", pf)


async def summary_loop():
    """Schickt einmal am Tag zur gewählten Stunde die Zusammenfassung."""
    await asyncio.sleep(30)
    while True:
        try:
            cfg = notify.config()
            now = dt.datetime.now(stats.TZ)
            today = now.date().isoformat()
            if ("summary" in cfg["events"] and notify.enabled(cfg) and now.hour >= cfg["summary_hour"]
                    and db.get_setting("summary_sent", "") != today):
                title, msg = summary_text()
                notify.send("summary", title, msg, priority=2, tags=["bar_chart"])
                db.set_setting("summary_sent", today)
        except Exception:
            log.exception("Fehler bei der Tageszusammenfassung")
        try:
            check_expiry()
        except Exception:
            log.exception("Fehler bei der Ablauf-Prüfung")
        await asyncio.sleep(300)
