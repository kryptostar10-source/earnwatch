"""Empfängt den Container-Status vom Host-Wächter (healthcheck.sh) über `docker compose exec`.

Aufruf nur von innen:  python -m earnwatch.hostevent '<json>'
Es gibt dafür keinen Netzwerk-Zugang – nur wer Docker auf dem Host bedienen darf, kann das auslösen.
Alle Eingaben werden streng geprüft (Namen, Zustände, Längen).
"""
from __future__ import annotations

import json
import re
import sys
import time

from . import db, notify

NAME_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
STATES = {"running", "exited", "dead", "created", "paused", "restarting", "removing", "missing", "unknown"}
HEALTH = {"", "healthy", "unhealthy", "starting"}
KINDS = {"down", "recovered", "crashed"}
LABELS = {"earnwatch": "EarnWatch", "caddy": "Caddy (HTTPS)", "tailscale": "Tailscale", "honeygain": "Honeygain",
          "earnapp": "EarnApp", "pawns": "Pawns.app", "earnfm": "Earn.fm"}
STATE_DE = {"exited": "gestoppt", "dead": "abgestürzt", "created": "nicht gestartet", "paused": "pausiert",
            "restarting": "startet ständig neu", "missing": "fehlt", "unknown": "unbekannt"}


def handle(raw: str) -> int:
    if len(raw) > 20000:
        return 2
    data = json.loads(raw)
    services = []
    for s in (data.get("services") or [])[:30]:
        name, state, health = str(s.get("name", "")), str(s.get("state", "")), str(s.get("health", ""))
        if not NAME_RE.match(name) or state not in STATES or health not in HEALTH:
            continue
        services.append({"name": name, "label": LABELS.get(name, name), "state": state, "health": health,
                         "restarts": max(0, min(int(s.get("restarts") or 0), 10**6))})
    db.set_setting("containers", json.dumps({"ts": int(time.time()), "services": services}))

    for e in (data.get("events") or [])[:50]:
        name, kind = str(e.get("name", "")), str(e.get("kind", ""))
        if not NAME_RE.match(name) or kind not in KINDS:
            continue
        label = LABELS.get(name, name)
        detail = str(e.get("detail", ""))[:40]
        if kind == "down":
            st = detail.split("/")[0]
            how = "wurde automatisch neu gestartet ✅" if e.get("action") in ("started", "restarted") else "konnte NICHT neu gestartet werden ❌"
            notify.send("container", f"🐳 {label} war {STATE_DE.get(st, 'nicht erreichbar' if 'unhealthy' in detail else st)}",
                        f"Der Container {how}", priority=4, tags=["whale"], wait=True)
        elif kind == "crashed":
            n = int(detail) if detail.isdigit() else 1
            notify.send("container", f"🐳 {label} ist abgestürzt", f"Docker hat ihn {n}× neu gestartet. Läuft wieder.",
                        priority=3, tags=["whale"], wait=True)
        elif kind == "recovered":
            notify.send("container", f"🐳 {label} läuft wieder", "Alles wieder in Ordnung.", priority=2, tags=["whale"], wait=True)
    return 0


if __name__ == "__main__":
    db.init()
    try:
        sys.exit(handle(sys.argv[1] if len(sys.argv) > 1 else "{}"))
    except (ValueError, TypeError, AttributeError):
        sys.exit(2)
