"""Auswertung: aus Kontostand-Verläufen werden Einnahmen je Tag/Woche/Monat/Jahr."""
from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from zoneinfo import ZoneInfo

from . import config, db
from .collectors import PLATFORMS

try:
    TZ = ZoneInfo(config.TZ)
except Exception:  # pragma: no cover
    TZ = ZoneInfo("UTC")

MONTHS = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]


def day_of(ts: int) -> dt.date:
    return dt.datetime.fromtimestamp(ts, TZ).date()


def usd_factor(pf: str) -> float | None:
    """Umrechnung Einheit -> USD (None = kein Geldwert, z. B. Grass-Punkte)."""
    mode = PLATFORMS[pf]["usd"]
    if mode is True:
        return 1.0
    if mode == "price":
        try:
            p = float(db.get_setting(f"price_{pf}", "0"))
            return p if p > 0 else None
        except ValueError:
            return None
    return None


def _week_start(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def buckets(view: str, today: dt.date, first: dt.date | None):
    """Liefert [(label, start, ende_exkl.)] für das Diagramm."""
    out = []
    if view == "week":
        ws = _week_start(today)
        for i in range(11, -1, -1):
            s = ws - dt.timedelta(weeks=i)
            out.append((f"KW {s.isocalendar()[1]}", s, s + dt.timedelta(days=7)))
    elif view == "month":
        y, m = today.year, today.month
        for i in range(11, -1, -1):
            mm, yy = m - i, y
            while mm <= 0:
                mm += 12
                yy -= 1
            s = dt.date(yy, mm, 1)
            e = dt.date(yy + (mm == 12), mm % 12 + 1, 1)
            out.append((f"{MONTHS[mm - 1]} {str(yy)[2:]}", s, e))
    elif view == "year":
        y0 = first.year if first else today.year
        for y in range(y0, today.year + 1):
            out.append((str(y), dt.date(y, 1, 1), dt.date(y + 1, 1, 1)))
    else:  # day
        for i in range(29, -1, -1):
            d = today - dt.timedelta(days=i)
            out.append((d.strftime("%d.%m."), d, d + dt.timedelta(days=1)))
    return out


def compute(view: str = "day", period: str = "week") -> dict:
    rows = db.snapshots()
    by_pf: dict[str, list] = defaultdict(list)
    for r in rows:
        by_pf[r["platform"]].append((r["ts"], r["balance"]))

    today = dt.datetime.now(TZ).date()
    daily: dict[str, dict[dt.date, float]] = {}
    payouts: list[dict] = []
    first_day = None
    for pf, pts in by_pf.items():
        on_drop = PLATFORMS.get(pf, {}).get("on_drop", "payout")
        d = defaultdict(float)
        for (t0, b0), (t1, b1) in zip(pts, pts[1:]):
            delta = b1 - b0
            if delta >= 0:
                d[day_of(t1)] += delta
            elif on_drop == "reset":
                d[day_of(t1)] += max(b1, 0)   # neue Epoche: Stand beginnt wieder bei 0
            elif delta < -0.005:
                payouts.append({"platform": pf, "ts": t1, "amount": -delta})
        daily[pf] = d
        if pts:
            fd = day_of(pts[0][0])
            first_day = fd if first_day is None or fd < first_day else first_day

    # Vom Anbieter gelieferte Tageswerte (z. B. Honeygain, letzte 30 Tage) einrechnen:
    # vor dem ersten eigenen Messwert ersetzen sie die fehlenden Daten, am ersten Messtag
    # gilt der größere Wert (unser erster Tag ist ja nur teilweise gemessen).
    for pf, days in db.imported_history().items():
        pts = by_pf.get(pf, [])
        first = day_of(pts[0][0]) if pts else today + dt.timedelta(days=1)
        d = daily.setdefault(pf, defaultdict(float))
        for ds, amount in days.items():
            try:
                day = dt.date.fromisoformat(ds)
            except ValueError:
                continue
            if day < first:
                d[day] = amount
            elif day == first:
                d[day] = max(d.get(day, 0.0), amount)
            if first_day is None or day < first_day:
                first_day = day

    def period_sum(pf: str, start: dt.date, end: dt.date) -> float:
        return sum(v for k, v in daily.get(pf, {}).items() if start <= k < end)

    tomorrow = today + dt.timedelta(days=1)
    periods = {
        "today": (today, tomorrow),
        "week": (_week_start(today), tomorrow),
        "month": (today.replace(day=1), tomorrow),
        "year": (today.replace(month=1, day=1), tomorrow),
        "d7": (today - dt.timedelta(days=6), tomorrow),
    }

    # Vergleichszeitraum: gleich lange Strecke davor (z. B. Mo–Sa letzte Woche)
    def _shift_month(d: dt.date, n: int) -> dt.date:
        y, m = d.year, d.month + n
        while m <= 0:
            m += 12
            y -= 1
        last = (dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1)).day
        return dt.date(y, m, min(d.day, last))

    def _shift_year(d: dt.date) -> dt.date:
        try:
            return d.replace(year=d.year - 1)
        except ValueError:  # 29. Februar
            return d.replace(year=d.year - 1, day=28)

    prev_periods = {
        "today": (today - dt.timedelta(days=1), today),
        "week": (periods["week"][0] - dt.timedelta(days=7), tomorrow - dt.timedelta(days=7)),
        "month": (_shift_month(periods["month"][0], -1), _shift_month(today, -1) + dt.timedelta(days=1)),
        "year": (_shift_year(periods["year"][0]), _shift_year(today) + dt.timedelta(days=1)),
    }
    elapsed = {"today": 1, "week": today.weekday() + 1, "month": today.day,
               "year": today.timetuple().tm_yday}

    configured = db.configured_platforms()
    runs = db.last_runs()
    platforms = []
    totals = {k: 0.0 for k in ("balance", "today", "week", "month", "year", "all", "forecast", "paid_out",
                               "prev_today", "prev_week", "prev_month", "prev_year")}
    for pf, meta in PLATFORMS.items():
        pts = by_pf.get(pf, [])
        fac = usd_factor(pf)
        bal = pts[-1][1] if pts else None
        p = {
            "id": pf, "name": meta["name"], "color": meta["color"], "unit": meta["unit"], "short": meta["short"],
            "dashboard": meta["dashboard"],
            "configured": pf in configured or (meta.get("fallback_creds") in configured and bool(pts))
                          or (bool(meta.get("manual")) and bool(pts)),
            "manual": bool(meta.get("manual")),
            "balance": bal, "last_ts": pts[-1][0] if pts else None,
            "fac": fac, "run": runs.get(pf), "lifetime": bool(meta.get("lifetime")), "min_payout": meta["min_payout"],
            "all": sum(daily.get(pf, {}).values()),
            "paid_out": sum(x["amount"] for x in payouts if x["platform"] == pf),
        }
        for k, (s, e) in periods.items():
            p[k] = period_sum(pf, s, e)
        for k, (s, e) in prev_periods.items():
            p["prev_" + k] = period_sum(pf, s, e)
        p["sel"] = p.get(period, 0.0)
        p["spark"] = [daily.get(pf, {}).get(today - dt.timedelta(days=i), 0.0) for i in range(13, -1, -1)]
        p["forecast"] = p["d7"] / 7 * 30
        p["progress"] = min(1.0, (bal or 0) / meta["min_payout"]) if meta["min_payout"] else None
        try:
            p["info"] = json.loads(db.get_setting(f"info_{pf}", "{}"))
        except ValueError:
            p["info"] = {}
        if fac is not None and p["configured"]:
            if not meta.get("lifetime"):  # JumpTask: Summe aller Einnahmen, kein Guthaben
                totals["balance"] += (bal or 0) * fac
            for k in ("today", "week", "month", "year", "all", "forecast", "paid_out",
                      "prev_today", "prev_week", "prev_month", "prev_year"):
                totals[k] += p[k] * fac
        platforms.append(p)

    dev = {"online": 0, "total": 0, "known": False}
    for p in platforms:
        inf = p["info"] if p["configured"] else {}
        if isinstance(inf.get("geraete"), int):
            dev["known"] = True
            dev["total"] += inf["geraete"]
            dev["online"] += inf["online"] if isinstance(inf.get("online"), int) else inf["geraete"]

    chart = []
    for label, s, e in buckets(view, today, first_day):
        vals = {}
        for pf in PLATFORMS:
            fac = usd_factor(pf)
            if fac is not None:
                vals[pf] = period_sum(pf, s, e) * fac
        chart.append({"label": label, "values": vals, "start": s})

    since = today - dt.timedelta(days=90)
    balance_series = {
        pf: [(t, b * usd_factor(pf)) for t, b in pts if day_of(t) >= since]
        for pf, pts in by_pf.items() if pf in PLATFORMS and usd_factor(pf) is not None
    }
    payouts.sort(key=lambda x: x["ts"], reverse=True)

    if period not in periods:
        period = "week"
    cur, prev = totals[period], totals["prev_" + period]
    growth = ((cur / prev - 1) * 100) if prev > 0.0005 else None
    usd_pfs = [p for p in platforms if p["fac"] is not None and p["configured"]]
    share = [{"id": p["id"], "color": p["color"], "name": p["name"], "value": p["sel"] * p["fac"]}
             for p in usd_pfs if p["sel"] > 0]
    hero_spark = []
    for i in range(29, -1, -1):
        d = today - dt.timedelta(days=i)
        hero_spark.append(sum(daily.get(p["id"], {}).get(d, 0.0) * p["fac"] for p in usd_pfs))

    # Aktivitäts-Feed: letzte Einnahmen, Auszahlungen, Fehler
    feed = []
    for p in platforms:
        pts = by_pf.get(p["id"], [])
        if len(pts) >= 2 and pts[-1][1] > pts[-2][1]:
            feed.append({"ts": pts[-1][0], "pf": p["id"], "name": p["name"], "kind": "earn",
                         "amount": pts[-1][1] - pts[-2][1], "unit": p["unit"]})
        r = p["run"]
        if r is not None and not r["ok"] and p["configured"]:
            feed.append({"ts": r["ts"], "pf": p["id"], "name": p["name"], "kind": "error", "text": r["message"]})
    for x in payouts[:5]:
        feed.append({"ts": x["ts"], "pf": x["platform"], "name": PLATFORMS[x["platform"]]["name"], "kind": "payout",
                     "amount": x["amount"], "unit": PLATFORMS[x["platform"]]["unit"]})
    feed.sort(key=lambda f: f["ts"], reverse=True)

    return {
        "period": period,
        "devices": dev,
        "growth": growth,
        "avg_day": cur / elapsed[period],
        "share": share,
        "hero_spark": hero_spark,
        "feed": feed[:6],
        "platforms": platforms,
        "chart": chart,
        "balance_series": balance_series,
        "payouts": payouts[:25],
        "totals": totals,
        "has_data": bool(rows),
        "points_platforms": [p for p in platforms if p["fac"] is None and p["configured"]],
        "_daily": daily, "_by_pf": by_pf, "_all_payouts": payouts, "_first_day": first_day, "_today": today,
    }


WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def platform_detail(pf: str, view: str = "day", period: str = "week") -> dict:
    """Alles für die Detailseite einer einzelnen Plattform (Beträge in ihrer eigenen Einheit)."""
    base = compute(view, period)
    p = next(x for x in base["platforms"] if x["id"] == pf)
    daily = base["_daily"].get(pf, {})
    pts = base["_by_pf"].get(pf, [])
    today = base["_today"]
    first = min(daily) if daily else (day_of(pts[0][0]) if pts else None)

    bars = []
    for label, s, e in buckets(view, today, first):
        v = sum(val for k, val in daily.items() if s <= k < e)
        bars.append({"label": label, "value": v, "start": s, "future": first is None or e <= first})
    measured = [b["value"] for b in bars if not b["future"]]
    avg = sum(measured) / len(measured) if measured else 0.0

    last30 = [(today - dt.timedelta(days=i)) for i in range(29, -1, -1)]
    best = max(((d, v) for d, v in daily.items()), key=lambda x: x[1], default=None)
    active_days = sum(1 for d in last30 if daily.get(d, 0) > 0)

    # Ø je Wochentag über die letzten 8 Wochen
    wd_sum, wd_n = [0.0] * 7, [0] * 7
    for i in range(56):
        d = today - dt.timedelta(days=i)
        if first and d >= first:
            wd_sum[d.weekday()] += daily.get(d, 0.0)
            wd_n[d.weekday()] += 1
    weekday = [{"label": WEEKDAYS[i], "value": (wd_sum[i] / wd_n[i]) if wd_n[i] else 0.0} for i in range(7)]

    since = today - dt.timedelta(days=90)
    balance = [(t, b) for t, b in pts if day_of(t) >= since]
    payouts = [x for x in base["_all_payouts"] if x["platform"] == pf]
    cur, prev = p.get(period, 0.0), p.get("prev_" + period, 0.0)
    return {
        "p": p, "bars": bars, "avg": avg, "best": best, "active_days": active_days,
        "weekday": weekday, "balance": balance, "payouts": payouts[:25],
        "growth": ((cur / prev - 1) * 100) if prev > 0 else None,
        "since": first, "measurements": len(pts),
    }
