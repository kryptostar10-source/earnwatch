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


def compute(view: str = "day") -> dict:
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

    configured = db.configured_platforms()
    runs = db.last_runs()
    platforms = []
    totals = {k: 0.0 for k in ("balance", "today", "week", "month", "year", "all", "forecast")}
    for pf, meta in PLATFORMS.items():
        pts = by_pf.get(pf, [])
        fac = usd_factor(pf)
        bal = pts[-1][1] if pts else None
        p = {
            "id": pf, "name": meta["name"], "color": meta["color"], "unit": meta["unit"],
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
        p["forecast"] = p["d7"] / 7 * 30
        p["progress"] = min(1.0, (bal or 0) / meta["min_payout"]) if meta["min_payout"] else None
        try:
            p["info"] = json.loads(db.get_setting(f"info_{pf}", "{}"))
        except ValueError:
            p["info"] = {}
        if fac is not None and p["configured"]:
            if not meta.get("lifetime"):  # JumpTask: Summe aller Einnahmen, kein Guthaben
                totals["balance"] += (bal or 0) * fac
            for k in ("today", "week", "month", "year", "all", "forecast"):
                totals[k] += p[k] * fac
        platforms.append(p)

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
    return {
        "platforms": platforms,
        "chart": chart,
        "balance_series": balance_series,
        "payouts": payouts[:25],
        "totals": totals,
        "has_data": bool(rows),
        "points_platforms": [p for p in platforms if p["fac"] is None and p["configured"]],
    }
