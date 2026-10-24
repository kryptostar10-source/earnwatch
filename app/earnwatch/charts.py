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


def money(v: float) -> str:
    sign = "-" if v < 0 else ""
    v = abs(v)
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
