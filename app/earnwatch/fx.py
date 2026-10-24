"""Wechselkurs USD→EUR automatisch von der Europäischen Zentralbank (einmal täglich)."""
from __future__ import annotations

import asyncio
import logging
import re
import time

from . import charts, db
from .collectors import CollectError, _opener, _request

log = logging.getLogger("earnwatch.fx")
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
_USD_RE = re.compile(rb"currency=['\"]USD['\"]\s+rate=['\"]([0-9]{1,3}\.[0-9]{1,8})['\"]")


def fetch_rate() -> float | None:
    """Gibt EUR pro 1 USD zurück (oder None). Kein XML-Parser nötig – nur ein fester Suchmuster."""
    try:
        st, raw, _ = _request(_opener(), ECB_URL, accept="application/xml,text/xml")
    except CollectError as e:
        log.warning("EZB-Kurs nicht erreichbar: %s", e)
        return None
    if st != 200:
        log.warning("EZB-Kurs: HTTP %s", st)
        return None
    m = _USD_RE.search(raw[:200_000])
    if not m:
        return None
    usd_per_eur = float(m.group(1))
    if not 0.5 < usd_per_eur < 2.5:   # Plausibilität
        return None
    return round(1 / usd_per_eur, 6)


def update_now() -> float | None:
    rate = fetch_rate()
    if rate:
        db.set_setting("eur_rate", str(rate))
        db.set_setting("eur_rate_ts", str(int(time.time())))
        charts.reset_currency_cache()
    return rate


async def fx_loop():
    """Holt den Kurs beim Start und dann alle 12 Stunden (nur wenn Euro gewählt ist)."""
    from starlette.concurrency import run_in_threadpool
    await asyncio.sleep(20)
    while True:
        try:
            last = int(db.get_setting("eur_rate_ts", "0") or 0)
            if db.get_setting("currency", "USD") == "EUR" and time.time() - last > 12 * 3600:
                await run_in_threadpool(update_now)
        except Exception:
            log.exception("Fehler beim Kursabruf")
        await asyncio.sleep(1800)
