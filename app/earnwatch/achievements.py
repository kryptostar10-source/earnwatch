"""Erfolge zum Freischalten – berechnet aus den eigenen Daten, gespeichert mit Datum."""
from __future__ import annotations

import json
import logging
import time
from collections import defaultdict

from . import charts, db, notify

log = logging.getLogger("earnwatch.achievements")


def _ctx(d: dict) -> dict:
    heat = d["heat"]
    months = defaultdict(float)
    for day, v in heat.items():
        months[(day.year, day.month)] += v
    dev_on = d["devices"]["online"] if d["devices"]["known"] else 0
    duino = next((p for p in d["platforms"] if p["id"] == "duinocoin" and p["configured"]), None)
    first = min((day for day, v in heat.items() if v > 0), default=None)
    return {
        "all": d["totals"]["all"],
        "payouts": len(d.get("_all_payouts", [])),
        "longest": d["streaks"]["longest"],
        "best_day": max(heat.values(), default=0.0),
        "best_month": max(months.values(), default=0.0),
        "platforms": sum(1 for p in d["platforms"] if p["configured"]),
        "devices": dev_on,
        "miners": int((duino or {}).get("info", {}).get("miner", 0) or 0),
        "days": (max(heat) - first).days + 1 if first else 0,
    }


# (id, Symbol, Titel, Beschreibung, Schlüssel, Ziel, ist_Geld)
DEFS = [
    ("first_cent", "🌱", "Erster Cent", "Die erste Einnahme ist da", "all", 0.01, True),
    ("usd_1", "🪙", "Der erste Dollar", "Insgesamt 1 $ verdient", "all", 1, True),
    ("usd_10", "💵", "Zehner", "Insgesamt 10 $ verdient", "all", 10, True),
    ("usd_50", "💰", "Fünfziger", "Insgesamt 50 $ verdient", "all", 50, True),
    ("usd_100", "🏆", "Hunderter", "Insgesamt 100 $ verdient", "all", 100, True),
    ("usd_500", "💎", "Fünfhundert", "Insgesamt 500 $ verdient", "all", 500, True),
    ("usd_1000", "👑", "Tausender", "Insgesamt 1.000 $ verdient", "all", 1000, True),
    ("day_1", "⚡", "Ein-Dollar-Tag", "An einem Tag mehr als 1 $ verdient", "best_day", 1, True),
    ("month_10", "📅", "Zweistelliger Monat", "In einem Monat 10 $ verdient", "best_month", 10, True),
    ("month_50", "🚀", "Rakete", "In einem Monat 50 $ verdient", "best_month", 50, True),
    ("payout_1", "🎉", "Zahltag", "Erste Auszahlung erkannt", "payouts", 1, False),
    ("payout_5", "🏦", "Stammkunde", "5 Auszahlungen erhalten", "payouts", 5, False),
    ("streak_7", "🔥", "Eine Woche am Stück", "7 Tage hintereinander Einnahmen", "longest", 7, False),
    ("streak_30", "🔥", "Ein Monat am Stück", "30 Tage hintereinander Einnahmen", "longest", 30, False),
    ("streak_100", "☄️", "Unaufhaltsam", "100 Tage hintereinander Einnahmen", "longest", 100, False),
    ("streak_365", "🌟", "Ein ganzes Jahr", "365 Tage hintereinander Einnahmen", "longest", 365, False),
    ("platforms_5", "🧩", "Sammler", "5 Plattformen gleichzeitig aktiv", "platforms", 5, False),
    ("devices_10", "🖥️", "Gerätepark", "10 Geräte gleichzeitig online", "devices", 10, False),
    ("miners_7", "⛏️", "Mining-Rig", "7 Duino-Miner gleichzeitig online", "miners", 7, False),
    ("year_1", "🎂", "Jubiläum", "Ein Jahr mit EarnWatch dabei", "days", 365, False),
]


def _load() -> dict | None:
    raw = db.get_setting("achievements", "")
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return {k: int(v) for k, v in data.items() if isinstance(k, str) and isinstance(v, (int, float))}
    except ValueError:
        return {}


def evaluate(d: dict, send: bool = True) -> list[str]:
    """Prüft alle Erfolge, speichert neue mit Datum und meldet sie (beim allerersten Mal still)."""
    ctx = _ctx(d)
    stored = _load()
    first_run = stored is None
    got = dict(stored or {})
    new = []
    now = int(time.time())
    for aid, icon, title, _desc, key, goal, _money in DEFS:
        if aid not in got and ctx[key] >= goal:
            got[aid] = now
            new.append(aid)
    if new or first_run:
        db.set_setting("achievements", json.dumps(got))
    if new and send and not first_run:
        names = [f"{a[1]} {a[2]}" for a in DEFS if a[0] in new]
        notify.send("achievement", "🏅 Erfolg freigeschaltet!" if len(new) == 1 else f"🏅 {len(new)} Erfolge freigeschaltet!",
                    "\n".join(names[:6]), priority=3, tags=["medal"])
    return new


def overview(d: dict) -> dict:
    ctx = _ctx(d)
    got = _load() or {}
    items = []
    for aid, icon, title, desc, key, goal, is_money in DEFS:
        val = ctx[key]
        items.append({
            "id": aid, "icon": icon, "title": title, "desc": desc, "unlocked": got.get(aid),
            "progress": min(1.0, val / goal) if goal else 1.0,
            "val": charts.money(val) if is_money else f"{int(val):,}".replace(",", "."),
            "goal": charts.money(goal) if is_money else f"{int(goal):,}".replace(",", "."),
        })
    done = [i for i in items if i["unlocked"]]
    done.sort(key=lambda i: -i["unlocked"])
    nxt = sorted((i for i in items if not i["unlocked"]), key=lambda i: -i["progress"])
    return {"all": items, "done": done, "next": nxt, "count": len(done), "total": len(items)}
