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
        await asyncio.sleep(300)
