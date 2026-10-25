"""Hilfe für zugangsdaten.sh (läuft im EarnWatch-Container, Daten nur über stdin/stdout, nie als Argument):

  python -m earnwatch.credtool test-honeygain   < {"email": "...", "password": "..."}  -> OK / FALSCH / ...
  python -m earnwatch.credtool test-pawns       < {"email": "...", "password": "..."}
  python -m earnwatch.credtool export honeygain -> gespeicherte E-Mail + Passwort aus den EarnWatch-Einstellungen
"""
from __future__ import annotations

import json
import secrets
import string
import sys

from . import db
from .collectors import CollectError, HG_API, PAWNS_API, _json, _opener, _request


def _test(url: str, body: dict, ok=(200, 201)) -> str:
    try:
        st, raw, _ = _request(_opener(), url, method="POST", body=body)
    except CollectError as e:
        return f"NETZWERK {e}"
    if st in ok:
        return "OK"
    if st == 429:
        return "GESPERRT (zu viele Versuche – später nochmal)"
    if st in (400, 401, 403, 422):
        try:
            detail = json.dumps(_json(raw))[:160] if raw else ""
        except CollectError:
            detail = ""
        return "FALSCH " + detail
    return f"HTTP {st}"


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd in ("test-honeygain", "test-pawns"):
        data = json.loads(sys.stdin.read(10000) or "{}")
        email, pw = str(data.get("email", "")), str(data.get("password", ""))
        if cmd == "test-honeygain":
            print(_test(f"{HG_API}/v1/users/tokens", {"email": email, "password": pw}))
        else:
            ident = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(21))
            print(_test(f"{PAWNS_API}/users/tokens", {"identifier": ident, "email": email, "password": pw,
                                                       "h_captcha_response": ""}))
        return 0
    if cmd == "export" and len(sys.argv) > 2 and sys.argv[2] in ("honeygain", "pawns"):
        db.init()
        c = db.load_credentials(sys.argv[2]) or {}
        if not c.get("email") or not c.get("password"):
            return 3
        # NUL-getrennt, damit jedes Zeichen im Passwort erhalten bleibt
        sys.stdout.write(c["email"] + "\0" + c["password"] + "\0")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
