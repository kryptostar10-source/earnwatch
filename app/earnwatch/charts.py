"""Diagramme als reines SVG (kein JavaScript, keine externen Bibliotheken)."""
from __future__ import annotations

import datetime as dt
from html import escape
from markupsafe import Markup

from .collectors import PLATFORMS
from .stats import TZ


def _nice_max(v: float) -> float:
    if v <= 0:
        return 0.05
    for step in (0.01, 0.02, 0.05, 0.1, 0.2, 0.25, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500):
        if v <= step * 4:
            return step * 4
    return v * 1.1


_cur = {"ts": 0.0, "code": "USD", "rate": 1.0}


def currency() -> tuple[str, float]:
    """Anzeige-Währung (USD oder EUR mit automatischem EZB-Kurs), 30 s zwischengespeichert."""
    import time
    from . import db
    if time.time() - _cur["ts"] > 30:
        code = db.get_setting("currency", "USD")
        try:
            rate = float(db.get_setting("eur_rate", "0") or 0)
        except ValueError:
            rate = 0.0
        if code == "EUR" and 0.3 < rate < 3:
            _cur.update(code="EUR", rate=rate)
        else:
            _cur.update(code="USD", rate=1.0)
        _cur["ts"] = time.time()
    return _cur["code"], _cur["rate"]


def reset_currency_cache() -> None:
    _cur["ts"] = 0.0


def money(v: float) -> str:
    """USD-Betrag in der gewählten Anzeige-Währung formatieren."""
    code, rate = currency()
    sign = "-" if v < 0 else ""
    v = abs(v) * rate
    if code == "EUR":
        txt = f"{v:,.2f}" if v >= 1 else f"{v:.3f}"
        return sign + txt.replace(",", "X").replace(".", ",").replace("X", ".") + " €"
    return f"{sign}${v:,.2f}" if v >= 1 else f"{sign}${v:.3f}"


def amount(v: float | None, unit: str) -> str:
    """Betrag in seiner Einheit formatieren."""
    if v is None:
        return "–"
    if unit == "USD":
        return money(v)
    if unit == "DUCO":
        return f"{v:,.2f} DUCO" if abs(v) >= 10 else f"{v:.3f} DUCO"
    return f"{v:,.0f} Pkt" if abs(v) >= 100 else f"{v:,.1f} Pkt"


def bar_chart(bars: list[dict], width=860, height=280) -> Markup:
    """Gestapelte Säulen je Plattform (USD)."""
    pad_l, pad_r, pad_t, pad_b = 52, 6, 10, 28
    w, h = width - pad_l - pad_r, height - pad_t - pad_b
    totals = [sum(b["values"].values()) for b in bars]
    vmax = _nice_max(max(totals) if totals else 0)
    n = len(bars) or 1
    slot = w / n
    bw = max(3.0, min(slot * 0.62, 46))
    out = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="Einnahmen je Zeitraum">']
    for i in range(5):
        y = pad_t + h - h * i / 4
        out.append(f'<line x1="{pad_l}" x2="{width - pad_r}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>')
        out.append(f'<text x="{pad_l - 10}" y="{y + 4:.1f}" class="axis" text-anchor="end">{money(vmax * i / 4)}</text>')
    for i, b in enumerate(bars):
        x = pad_l + i * slot + (slot - bw) / 2
        y = pad_t + h
        tip = [b["label"]]
        for pf, v in b["values"].items():
            if v <= 0:
                continue
            bh = max(h * v / vmax, 1.5)
            y -= bh
            out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{max(bh - 1.5, 1):.1f}" '
                       f'rx="3" fill="{PLATFORMS[pf]["color"]}"/>')
            tip.append(f'{PLATFORMS[pf]["name"]}: {money(v)}')
        tip.append(f"Summe: {money(totals[i])}")
        out.append(f'<rect x="{pad_l + i * slot:.1f}" y="{pad_t}" width="{slot:.1f}" height="{h}" '
                   f'class="hit"><title>{escape(chr(10).join(tip))}</title></rect>')
        if n <= 14 or i % max(1, n // 10) == 0 or i == n - 1:
            out.append(f'<text x="{pad_l + i * slot + slot / 2:.1f}" y="{height - 8}" class="axis" '
                       f'text-anchor="middle">{escape(b["label"])}</text>')
    out.append("</svg>")
    return Markup("".join(out))


def hero_spark(values: list[float], width=420, height=56) -> Markup:
    """Mini-Säulen der letzten 30 Tage für die Guthaben-Karte."""
    n = len(values) or 1
    vmax = max(values) if values and max(values) > 0 else 1
    gap = 3
    bw = (width - gap * (n - 1)) / n
    out = [f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" class="herospark" aria-hidden="true">']
    for i, v in enumerate(values):
        bh = max(3, height * (v / vmax))
        out.append(f'<rect x="{i * (bw + gap):.1f}" y="{height - bh:.1f}" width="{bw:.1f}" height="{bh:.1f}" rx="2"/>')
    out.append("</svg>")
    return Markup("".join(out))


def spark(values: list[float], color: str, width=96, height=34) -> Markup:
    n = len(values) or 1
    vmax = max(values) if values and max(values) > 0 else 1
    gap = 2.5
    bw = (width - gap * (n - 1)) / n
    out = [f'<svg viewBox="0 0 {width} {height}" class="spark" aria-hidden="true">']
    for i, v in enumerate(values):
        bh = max(2, height * (v / vmax)) if v > 0 else 2
        op = "0.9" if v > 0 else "0.25"
        out.append(f'<rect x="{i * (bw + gap):.1f}" y="{height - bh:.1f}" width="{bw:.1f}" height="{bh:.1f}" '
                   f'rx="1.5" fill="{color}" fill-opacity="{op}"/>')
    out.append("</svg>")
    return Markup("".join(out))


def share_bar(share: list[dict], width=400, height=10) -> Markup:
    total = sum(s["value"] for s in share)
    if total <= 0:
        return Markup(f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" class="sharebar" aria-hidden="true">'
                      f'<rect width="{width}" height="{height}" rx="5" class="empty"/></svg>')
    out = [f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" class="sharebar" role="img" '
           f'aria-label="Anteil je Plattform">']
    x = 0.0
    for s in share:
        w = width * s["value"] / total
        out.append(f'<rect x="{x:.2f}" y="0" width="{max(w - 2, 0.5):.2f}" height="{height}" fill="{s["color"]}">'
                   f'<title>{escape(s["name"])}: {money(s["value"])} ({s["value"] / total * 100:.0f} %)</title></rect>')
        x += w
    out.append("</svg>")
    return Markup("".join(out))


def miners(rigs: list[dict], width=320) -> Markup:
    if not rigs:
        return Markup('<p class="muted small">Keine Miner online.</p>')
    row = 22
    height = row * len(rigs)
    vmax = max(r["hashrate"] for r in rigs) or 1
    out = [f'<svg viewBox="0 0 {width} {height}" class="miners" role="img" aria-label="Hashrate je Miner">']
    for i, r in enumerate(rigs):
        y = i * row
        bw = (width - 170) * r["hashrate"] / vmax
        out.append(f'<text x="0" y="{y + 14}" class="mlabel">{escape(r["name"])}</text>')
        out.append(f'<rect x="96" y="{y + 7}" width="{width - 170}" height="6" rx="3" class="mtrack"/>')
        out.append(f'<rect x="96" y="{y + 7}" width="{max(bw, 3):.1f}" height="6" rx="3" fill="#fb923c"/>')
        out.append(f'<text x="{width}" y="{y + 14}" class="mval" text-anchor="end">{r["hashrate"] / 1000:.1f} kH/s</text>')
    out.append("</svg>")
    return Markup("".join(out))


def line_chart(series: dict[str, list], width=860, height=200) -> Markup:
    pad_l, pad_r, pad_t, pad_b = 46, 8, 10, 26
    w, h = width - pad_l - pad_r, height - pad_t - pad_b
    pts_all = [p for s in series.values() for p in s]
    if len(pts_all) < 2:
        return Markup('<p class="muted">Noch zu wenig Messpunkte für den Verlauf.</p>')
    t0 = min(p[0] for p in pts_all)
    t1 = max(p[0] for p in pts_all)
    t1 = t1 if t1 > t0 else t0 + 1
    vmax = _nice_max(max(p[1] for p in pts_all))
    out = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="Kontostand-Verlauf">']
    for i in range(5):
        v = vmax * i / 4
        y = pad_t + h - h * i / 4
        out.append(f'<line x1="{pad_l}" x2="{width - pad_r}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>')
        out.append(f'<text x="{pad_l - 6}" y="{y + 4:.1f}" class="axis" text-anchor="end">{money(v)}</text>')
    for k in range(5):
        t = t0 + (t1 - t0) * k / 4
        x = pad_l + w * k / 4
        lbl = dt.datetime.fromtimestamp(t, TZ).strftime("%d.%m.")
        anchor = "start" if k == 0 else "end" if k == 4 else "middle"
        out.append(f'<text x="{x:.1f}" y="{height - 8}" class="axis" text-anchor="{anchor}">{lbl}</text>')
    for pf, pts in series.items():
        if not pts:
            continue
        coords = " ".join(
            f"{pad_l + w * (t - t0) / (t1 - t0):.1f},{pad_t + h - h * b / vmax:.1f}" for t, b in pts
        )
        out.append(f'<polyline points="{coords}" fill="none" stroke="{PLATFORMS[pf]["color"]}" '
                   f'stroke-width="2" stroke-linejoin="round"/>')
        t, b = pts[-1]
        out.append(f'<circle cx="{pad_l + w * (t - t0) / (t1 - t0):.1f}" cy="{pad_t + h - h * b / vmax:.1f}" '
                   f'r="3.5" fill="{PLATFORMS[pf]["color"]}"><title>{PLATFORMS[pf]["name"]}: {money(b)}</title></circle>')
    out.append("</svg>")
    return Markup("".join(out))


# --- Detailseite einer Plattform -------------------------------------------
def _short(v: float, unit: str) -> str:
    """Kurze Achsenbeschriftung in der Einheit der Plattform."""
    if unit == "USD":
        return money(v) if v < 100 else money(round(v))
    if abs(v) >= 1000:
        return f"{v / 1000:.1f}k".replace(".0k", "k")
    return f"{v:.2f}" if abs(v) < 10 else f"{v:.0f}"


def _grad(gid: str, color: str, top: float, bottom: float) -> str:
    return (f'<defs><linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1">'
            f'<stop offset="0" stop-color="{color}" stop-opacity="{top}"/>'
            f'<stop offset="1" stop-color="{color}" stop-opacity="{bottom}"/></linearGradient></defs>')


def platform_bars(bars: list[dict], color: str, unit: str, avg: float, width=860, height=280) -> Markup:
    """Säulen einer Plattform mit Durchschnittslinie; der aktuelle Zeitraum leuchtet."""
    pad_l, pad_r, pad_t, pad_b = 56, 8, 14, 28
    w, h = width - pad_l - pad_r, height - pad_t - pad_b
    vmax = _nice_max(max((b["value"] for b in bars), default=0))
    n = len(bars) or 1
    slot = w / n
    bw = max(4.0, min(slot * 0.64, 48))
    out = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="Einnahmen je Zeitraum">',
           _grad("pbar", color, 1, 0.55)]
    for i in range(5):
        y = pad_t + h - h * i / 4
        out.append(f'<line x1="{pad_l}" x2="{width - pad_r}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>')
        out.append(f'<text x="{pad_l - 10}" y="{y + 4:.1f}" class="axis" text-anchor="end">{escape(_short(vmax * i / 4, unit))}</text>')
    for i, b in enumerate(bars):
        x = pad_l + i * slot + (slot - bw) / 2
        last = i == n - 1
        if b["value"] > 0:
            bh = max(h * b["value"] / vmax, 3)
            op = "1" if last else "0.72"
            out.append(f'<rect x="{x:.1f}" y="{pad_t + h - bh:.1f}" width="{bw:.1f}" height="{bh:.1f}" rx="{min(6, bw / 2):.1f}" '
                       f'fill="url(#pbar)" fill-opacity="{op}"/>')
        else:
            out.append(f'<rect x="{x:.1f}" y="{pad_t + h - 3:.1f}" width="{bw:.1f}" height="3" rx="1.5" class="nobar"/>')
        tip = f'{b["label"]}: {amount(b["value"], unit)}' if not b.get("future") else f'{b["label"]}: noch keine Daten'
        out.append(f'<rect x="{pad_l + i * slot:.1f}" y="{pad_t}" width="{slot:.1f}" height="{h}" class="hit">'
                   f'<title>{escape(tip)}</title></rect>')
        if n <= 14 or i % max(1, n // 10) == 0 or last:
            out.append(f'<text x="{pad_l + i * slot + slot / 2:.1f}" y="{height - 8}" class="axis{" now" if last else ""}" '
                       f'text-anchor="middle">{escape(b["label"])}</text>')
    if avg > 0:
        y = pad_t + h - h * avg / vmax
        out.append(f'<line x1="{pad_l}" x2="{width - pad_r}" y1="{y:.1f}" y2="{y:.1f}" class="avgline"/>')
        out.append(f'<text x="{width - pad_r - 4}" y="{y - 6:.1f}" class="avglbl" text-anchor="end">Ø {escape(amount(avg, unit))}</text>')
    out.append("</svg>")
    return Markup("".join(out))


def balance_area(points: list[tuple], color: str, unit: str, width=860, height=220) -> Markup:
    """Kontostand-Verlauf als Fläche."""
    if len(points) < 2:
        return Markup('<p class="muted small">Noch zu wenig Messpunkte für den Verlauf.</p>')
    pad_l, pad_r, pad_t, pad_b = 56, 10, 14, 26
    w, h = width - pad_l - pad_r, height - pad_t - pad_b
    t0, t1 = points[0][0], points[-1][0]
    t1 = t1 if t1 > t0 else t0 + 1
    vmax = _nice_max(max(b for _, b in points))
    X = lambda t: pad_l + w * (t - t0) / (t1 - t0)  # noqa: E731
    Y = lambda b: pad_t + h - h * max(b, 0) / vmax  # noqa: E731
    out = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="Kontostand-Verlauf">',
           _grad("parea", color, 0.42, 0)]
    for i in range(5):
        y = pad_t + h - h * i / 4
        out.append(f'<line x1="{pad_l}" x2="{width - pad_r}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>')
        out.append(f'<text x="{pad_l - 10}" y="{y + 4:.1f}" class="axis" text-anchor="end">{escape(_short(vmax * i / 4, unit))}</text>')
    for k in range(5):
        t = t0 + (t1 - t0) * k / 4
        anchor = "start" if k == 0 else "end" if k == 4 else "middle"
        out.append(f'<text x="{X(t):.1f}" y="{height - 8}" class="axis" text-anchor="{anchor}">'
                   f'{dt.datetime.fromtimestamp(t, TZ).strftime("%d.%m.")}</text>')
    line = " ".join(f"{X(t):.1f},{Y(b):.1f}" for t, b in points)
    out.append(f'<polygon points="{X(t0):.1f},{pad_t + h} {line} {X(points[-1][0]):.1f},{pad_t + h}" fill="url(#parea)"/>')
    out.append(f'<polyline points="{line}" fill="none" stroke="{color}" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>')
    t, b = points[-1]
    out.append(f'<circle cx="{X(t):.1f}" cy="{Y(b):.1f}" r="9" fill="{color}" fill-opacity="0.25"/>')
    out.append(f'<circle cx="{X(t):.1f}" cy="{Y(b):.1f}" r="4.5" fill="{color}" stroke="#fff" stroke-width="1.5">'
               f'<title>Aktuell: {escape(amount(b, unit))}</title></circle>')
    out.append("</svg>")
    return Markup("".join(out))


def weekday_bars(values: list[dict], color: str, unit: str, width=320, height=150) -> Markup:
    """Ø Einnahmen je Wochentag – zeigt, an welchen Tagen am meisten reinkommt."""
    pad_b = 22
    h = height - pad_b - 18
    vmax = max((v["value"] for v in values), default=0) or 1
    top = max(range(len(values)), key=lambda i: values[i]["value"]) if values else -1
    slot = width / (len(values) or 1)
    bw = min(slot * 0.58, 30)
    out = [f'<svg viewBox="0 0 {width} {height}" class="chart wd" role="img" aria-label="Durchschnitt je Wochentag">']
    for i, v in enumerate(values):
        x = i * slot + (slot - bw) / 2
        bh = max(h * v["value"] / vmax, 3)
        best = i == top and v["value"] > 0
        out.append(f'<rect x="{x:.1f}" y="{18 + h - bh:.1f}" width="{bw:.1f}" height="{bh:.1f}" rx="{bw / 2:.1f}" '
                   f'fill="{color}" fill-opacity="{1 if best else 0.38}"><title>{v["label"]}: Ø {escape(amount(v["value"], unit))}</title></rect>')
        if best:
            out.append(f'<text x="{x + bw / 2:.1f}" y="{18 + h - bh - 6:.1f}" class="wdtop" text-anchor="middle">'
                       f'{escape(_short(v["value"], unit))}</text>')
        out.append(f'<text x="{x + bw / 2:.1f}" y="{height - 4}" class="axis{" now" if best else ""}" text-anchor="middle">{v["label"]}</text>')
    out.append("</svg>")
    return Markup("".join(out))


def heatmap(days: dict, unit: str = "USD") -> Markup:
    """Kalender wie bei GitHub: jede Spalte eine Woche, je kräftiger, desto mehr verdient."""
    if not days:
        return Markup("")
    dates = sorted(days)
    end = dates[-1]
    start = end - dt.timedelta(days=end.weekday() + 52 * 7)   # Montag vor 52 Wochen
    vals = sorted(v for d, v in days.items() if v > 0 and d >= start)
    q = [vals[int(len(vals) * f)] for f in (0.25, 0.5, 0.75, 0.9)] if vals else []
    cell, gap, left, top = 12, 3, 26, 18
    weeks = (end - start).days // 7 + 1
    width, height = left + weeks * (cell + gap), top + 7 * (cell + gap)
    out = [f'<svg viewBox="0 0 {width} {height}" class="heat" role="img" aria-label="Einnahmen je Tag im letzten Jahr">']
    for row, lbl in ((0, "Mo"), (2, "Mi"), (4, "Fr")):
        out.append(f'<text x="0" y="{top + row * (cell + gap) + 10}" class="axis">{lbl}</text>')
    last_month = None
    months = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]
    for w in range(weeks):
        monday = start + dt.timedelta(weeks=w)
        if monday.month != last_month and monday.day <= 7:
            out.append(f'<text x="{left + w * (cell + gap)}" y="11" class="axis">{months[monday.month - 1]}</text>')
            last_month = monday.month
        for r in range(7):
            d = monday + dt.timedelta(days=r)
            if d > end:
                break
            v = days.get(d, 0.0)
            lvl = 0 if v <= 0 else 1 + sum(1 for t in q[1:] if v >= t)
            x, y = left + w * (cell + gap), top + r * (cell + gap)
            fill = f' class="h{lvl}"'
            tip = f'{d.strftime("%d.%m.%Y")}: {amount(v, unit) if v > 0 else "nichts"}'
            out.append(f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" rx="3"{fill}>'
                       f'<title>{escape(tip)}</title></rect>')
    out.append("</svg>")
    return Markup("".join(out))
