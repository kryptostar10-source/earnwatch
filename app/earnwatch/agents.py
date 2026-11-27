"""Rechner-Überwachung: kleine Mess-Programme (z. B. auf dem Windows-PC) melden CPU, RAM und Internet.

Sicherheit:
* Jeder Rechner hat einen eigenen 256-Bit-Schlüssel (verschlüsselt gespeichert). Er wird NIE übertragen –
  jede Meldung ist nur damit signiert: HMAC-SHA256(Schlüssel, "zeit|nonce|sha256(inhalt)").
* Nur frische Meldungen (±5 Min.), jede Nonce nur einmal, höchstens eine Meldung pro 20 s und Rechner.
* Inhalt höchstens 4 KB, nur feste Felder mit Grenzen. Gespeichert werden nur Zahlen (6 Std. + Tagessumme).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time

from . import db

SKEW = 300
KEEP = 6 * 3600
ID_RE = re.compile(r"^[a-f0-9]{12}$")
NAME_RE = re.compile(r"^[\w .\-äöüÄÖÜß]{1,32}$")
_seen: dict[str, float] = {}
_lock = threading.Lock()
_last_post: dict[str, float] = {}
_live: dict[str, dict] = {}        # aktuelle Werte je Rechner (nur im Speicher)
_last_save: dict[str, float] = {}  # wann zuletzt in die Datenbank geschrieben
MIN_GAP = 0.7                      # höchstens ~1 Meldung pro Sekunde
# Verdienst-Apps, die der Agent auf dem PC erkennt (Schlüssel -> Plattform in EarnWatch)
APPS = {"honeygain": ("honeygain", "Honeygain"), "jumptask": ("honeygain_jt", "JumpTask"),
        "earnapp": ("earnapp", "EarnApp"), "pawns": ("pawns", "Pawns.app"), "bitping": ("bitping", "Bitping"),
        "earnfm": ("earnfm", "Earn.fm"), "wipter": ("wipter", "Wipter"), "bytelixir": ("bytelixir", "Bytelixir"),
        "grass": ("grass", "Grass")}
_app_hist: dict[str, dict[str, list]] = {}   # je Rechner, je App: letzte 5 Min. CPU (nur im Speicher)
SAVE_EVERY = 60


def _list() -> list[dict]:
    try:
        d = json.loads(db.get_setting("agents", "[]") or "[]")
        return [a for a in d if isinstance(a, dict) and ID_RE.match(str(a.get("id", "")))] if isinstance(d, list) else []
    except (ValueError, TypeError):
        return []


def all_agents() -> list[dict]:
    return _list()


def create(name: str) -> tuple[str, str]:
    name = name.strip()
    if not NAME_RE.match(name):
        raise ValueError("Name: 1–32 Zeichen, nur Buchstaben, Zahlen, Leerzeichen, Punkt, Minus.")
    lst = _list()
    if len(lst) >= 20:
        raise ValueError("Höchstens 20 Rechner.")
    aid, key = secrets.token_hex(6), secrets.token_hex(32)
    db.save_credentials(f"_agent_{aid}", {"k": key})
    lst.append({"id": aid, "name": name, "created": int(time.time())})
    db.set_setting("agents", json.dumps(lst))
    return aid, key


def delete(aid: str) -> None:
    if not ID_RE.match(aid):
        return
    db.set_setting("agents", json.dumps([a for a in _list() if a["id"] != aid]))
    db.delete_credentials(f"_agent_{aid}")
    db.set_setting(f"agent_{aid}", "")


def _key(aid: str) -> bytes | None:
    d = db.load_credentials(f"_agent_{aid}")
    try:
        return bytes.fromhex(d["k"]) if d and d.get("k") else None
    except (ValueError, KeyError):
        return None


def signature(key: bytes, ts: str, nonce: str, body: bytes) -> str:
    return hmac.new(key, f"{ts}|{nonce}|{hashlib.sha256(body).hexdigest()}".encode(), hashlib.sha256).hexdigest()


def verify(aid: str, headers, body: bytes) -> bool:
    if not ID_RE.match(aid) or len(body) > 4096 or not any(a["id"] == aid for a in _list()):
        return False
    key = _key(aid)
    ts, nonce, sig = (str(headers.get(h, "")) for h in ("x-ew-ts", "x-ew-nonce", "x-ew-sig"))
    if not key or not ts.isdigit() or not re.fullmatch(r"[A-Za-z0-9]{16,64}", nonce) or not re.fullmatch(r"[0-9a-f]{64}", sig):
        return False
    if not hmac.compare_digest(signature(key, ts, nonce, body), sig):
        return False
    now = time.time()
    if abs(now - int(ts)) > SKEW:
        return False
    with _lock:
        for k in [k for k, t in _seen.items() if t < now - 2 * SKEW]:
            del _seen[k]
        if nonce in _seen or len(_seen) > 50000 or now - _last_post.get(aid, 0) < MIN_GAP:
            return False
        _seen[nonce] = now
        _last_post[aid] = now
    return True


def _num(v, lo, hi, cast=float):
    try:
        x = cast(v)
    except (TypeError, ValueError):
        return None
    return x if lo <= x <= hi else None


def _txt(v, n):
    return re.sub(r"[^\w .,:()\-+/@®™]", "", str(v or ""))[:n]


def record(aid: str, body: bytes) -> bool:
    try:
        d = json.loads(body)
    except ValueError:
        return False
    if not isinstance(d, dict):
        return False
    tnow = time.time()
    cur = {"t": tnow, "cpu": _num(d.get("cpu"), 0, 100), "mem_used": _num(d.get("mem_used"), 0, 2**50, int),
           "mem_total": _num(d.get("mem_total"), 1, 2**50, int), "rx": _num(d.get("rx"), 0, 2**62, int),
           "tx": _num(d.get("tx"), 0, 2**62, int)}
    prev = _live.get(aid)
    if prev and 0 < tnow - prev["t"] < 30:
        dt = tnow - prev["t"]
        for k, out in (("rx", "down"), ("tx", "up")):
            cur[out] = ((cur[k] - prev[k]) * 8 / dt) if None not in (cur[k], prev[k]) and cur[k] >= prev[k] else prev.get(out)
    apps = {}
    for a in (d.get("apps") or [])[:20] if isinstance(d.get("apps"), list) else []:
        if not isinstance(a, dict) or a.get("k") not in APPS:
            continue
        apps[a["k"]] = {"cpu": _num(a.get("cpu"), 0, 100), "mem": _num(a.get("mem"), 0, 2**44, int),
                        "io": _num(a.get("io"), 0, 2**40, int), "n": _num(a.get("n"), 0, 999, int)}
    cur["apps"] = apps if "apps" in d else (prev or {}).get("apps", {})
    h = _app_hist.setdefault(aid, {})
    for k, v in cur["apps"].items():
        lst = h.setdefault(k, [])
        lst.append((tnow, v["cpu"] or 0.0))
        del lst[:-300]
    _live[aid] = cur
    if tnow - _last_save.get(aid, 0) < SAVE_EVERY:
        return True   # nur live – Verlauf wird einmal pro Minute gespeichert
    _last_save[aid] = tnow
    now = int(tnow)
    p = [now, _num(d.get("cpu"), 0, 100), _num(d.get("mem_used"), 0, 2**50, int), _num(d.get("mem_total"), 1, 2**50, int),
         _num(d.get("rx"), 0, 2**62, int), _num(d.get("tx"), 0, 2**62, int)]
    st = state(aid)
    pts = [x for x in st.get("pts", []) if now - x[0] <= KEEP]
    # Tagessumme Internet aus den Zählern (Neustart des PCs = Zähler fängt bei 0 an → überspringen)
    day = time.strftime("%Y-%m-%d")
    if st.get("day") != day:
        st["day"], st["rx_day"], st["tx_day"] = day, 0, 0
    if pts and now - pts[-1][0] < 900:
        prev = pts[-1]
        for i, k in ((4, "rx_day"), (5, "tx_day")):
            if p[i] is not None and prev[i] is not None and p[i] >= prev[i]:
                st[k] = st.get(k, 0) + p[i] - prev[i]
    pts.append(p)
    st["pts"] = pts[-400:]
    if d.get("host") or not st.get("info"):
        st["info"] = {"host": _txt(d.get("host"), 40), "os": _txt(d.get("os"), 60), "cpu_name": _txt(d.get("cpu_name"), 60),
                      "cores": _num(d.get("cores"), 1, 1024, int), "uptime": _num(d.get("uptime"), 0, 10**9, int)}
    st["seen"] = now
    db.set_setting(f"agent_{aid}", json.dumps(st, separators=(",", ":")))
    return True


def state(aid: str) -> dict:
    try:
        d = json.loads(db.get_setting(f"agent_{aid}", "") or "{}")
        return d if isinstance(d, dict) else {}
    except (ValueError, TypeError):
        return {}


def overview() -> list[dict]:
    """Für das Dashboard: je Rechner aktuelle Werte + Verläufe der letzten Stunden."""
    out = []
    now = time.time()
    for a in _list():
        st = state(a["id"])
        pts = st.get("pts", [])
        last = pts[-1] if pts else None
        down = up = None
        rates = []
        for p0, p1 in zip(pts, pts[1:]):
            dt = p1[0] - p0[0]
            if 0 < dt < 900 and None not in (p0[4], p1[4], p0[5], p1[5]) and p1[4] >= p0[4] and p1[5] >= p0[5]:
                rates.append((p1[0], (p1[4] - p0[4]) * 8 / dt, (p1[5] - p0[5]) * 8 / dt))
        online = bool(st.get("seen")) and now - st["seen"] < 180
        if rates and online:
            down, up = rates[-1][1], rates[-1][2]
        lv = _live.get(a["id"])
        if lv and now - lv["t"] < 10:   # Live-Werte (jede Sekunde) haben Vorrang
            online = True
            last = [int(lv["t"]), lv["cpu"], lv["mem_used"], lv["mem_total"], lv["rx"], lv["tx"]]
            down, up = lv.get("down", down), lv.get("up", up)
        out.append({
            "id": a["id"], "name": a["name"], "online": online, "seen": st.get("seen"),
            "cpu": last[1] if last and online else None,
            "mem_used": last[2] if last else None, "mem_total": last[3] if last else None,
            "mem_pct": (100 * last[2] / last[3]) if last and last[2] is not None and last[3] else None,
            "down": down, "up": up,
            "rx_day": st.get("rx_day", 0) if st.get("day") == time.strftime("%Y-%m-%d") else 0,
            "tx_day": st.get("tx_day", 0) if st.get("day") == time.strftime("%Y-%m-%d") else 0,
            "cpu_hist": [(p[0], p[1]) for p in pts if p[1] is not None][-180:],
            "down_hist": [(t, d) for t, d, _ in rates][-180:],
            "info": st.get("info", {}),
        })
    return out


def fmt_bits(bps) -> str:
    if bps is None:
        return "–"
    for unit in ("bit/s", "kbit/s", "Mbit/s", "Gbit/s"):
        if bps < 1000 or unit == "Gbit/s":
            return f"{bps:.0f} {unit}" if unit == "bit/s" else f"{bps:.1f} {unit}"
        bps /= 1000
    return "–"


def fmt_bytes(n) -> str:
    if n is None:
        return "–"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return "–"


_TPL = __import__("pathlib").Path(__file__).with_name("agent_tpl") / "windows.ps1"


def windows_install(base_url: str, aid: str, key: str) -> str:
    """Ein Block zum Einfügen in PowerShell: legt den Agenten an, startet ihn und trägt ihn in den Autostart ein."""
    script = _TPL.read_text(encoding="utf-8").replace("__URL__", base_url).replace("__ID__", aid).replace("__KEY__", key)
    return (
        '$d = "$env:LOCALAPPDATA\\EarnWatchAgent"; New-Item -ItemType Directory -Force $d | Out-Null\n'
        "Set-Content -Path \"$d\\agent.ps1\" -Encoding UTF8 -Value @'\n" + script + "\n'@\n"
        '$lnk = (New-Object -ComObject WScript.Shell).CreateShortcut("$([Environment]::GetFolderPath(\'Startup\'))\\EarnWatch-Agent.lnk")\n'
        '$lnk.TargetPath = "powershell.exe"; $lnk.WindowStyle = 7\n'
        '$lnk.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$d\\agent.ps1`""; $lnk.Save()\n'
        'Start-Process powershell.exe -WindowStyle Hidden -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$d\\agent.ps1`""\n'
        'Write-Host "EarnWatch-Agent laeuft und startet ab jetzt mit Windows." -ForegroundColor Green\n'
    )


WINDOWS_UNINSTALL = (
    "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | Where-Object CommandLine -like '*EarnWatchAgent*' | "
    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }\n"
    "Remove-Item \"$([Environment]::GetFolderPath('Startup'))\\EarnWatch-Agent.lnk\", \"$env:LOCALAPPDATA\\EarnWatchAgent\" -Recurse -Force"
)


def live() -> list[dict]:
    """Kleine JSON-Antwort für die Seite (jede Sekunde abgefragt)."""
    out = []
    for o in overview():
        out.append({"id": o["id"], "online": o["online"],
                     "cpu": None if o["cpu"] is None else round(o["cpu"], 1),
                     "mem": None if o["mem_pct"] is None else round(o["mem_pct"], 1),
                     "mem_txt": f"{fmt_bytes(o['mem_used'])} / {fmt_bytes(o['mem_total'])}",
                     "down": fmt_bits(o["down"]), "up": fmt_bits(o["up"])})
    return out


def detail(aid: str) -> dict | None:
    """Alles für die Rechner-Seite: Gesamtwerte + Verbrauch je Verdienst-App."""
    o = next((x for x in overview() if x["id"] == aid), None)
    if not o:
        return None
    lv = _live.get(aid) or {}
    fresh = lv and time.time() - lv.get("t", 0) < 10
    running = (lv.get("apps") or {}) if fresh else {}
    apps = []
    for k, (pf, name) in APPS.items():
        v = running.get(k)
        apps.append({"k": k, "pf": pf, "name": name, "running": bool(v),
                     "cpu": v["cpu"] if v else None, "mem": v["mem"] if v else None, "io": v["io"] if v else None,
                     "hist": [(t, c) for t, c in _app_hist.get(aid, {}).get(k, []) if time.time() - t < 300]})
    apps.sort(key=lambda a: (not a["running"], -(a["cpu"] or 0)))
    scale = max([5.0] + [a["cpu"] or 0 for a in apps])   # Balken relativ (mind. 5 %), Zahl bleibt echt
    for a in apps:
        a["bar"] = None if a["cpu"] is None else round(100 * a["cpu"] / scale)
    top = max([a["cpu"] or 0 for a in apps] + [5.0])   # Balken relativ zur hungrigsten App (mind. 5 %)
    for a in apps:
        a["bar"] = None if a["cpu"] is None else round(100 * a["cpu"] / top)
    o["apps"] = apps
    o["has_apps"] = "apps" in lv
    return o


def live_detail(aid: str) -> dict | None:
    o = detail(aid)
    if not o:
        return None
    return {"id": aid, "online": o["online"],
            "cpu": None if o["cpu"] is None else round(o["cpu"], 1),
            "mem": None if o["mem_pct"] is None else round(o["mem_pct"], 1),
            "mem_txt": f"{fmt_bytes(o['mem_used'])} / {fmt_bytes(o['mem_total'])}",
            "down": fmt_bits(o["down"]), "up": fmt_bits(o["up"]),
            "apps": [{"k": a["k"], "running": a["running"], "cpu": None if a["cpu"] is None else round(a["cpu"], 1), "bar": a["bar"], "bar": a["bar"],
                      "mem": fmt_bytes(a["mem"]) if a["mem"] is not None else "–",
                      "io": (fmt_bytes(a["io"]) + "/s") if a["io"] is not None else "–"} for a in o["apps"]]}
