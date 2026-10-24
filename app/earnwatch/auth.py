"""Passwort-Hashing (scrypt), Login-Bremse und CSRF-Schutz."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque

# Höchstens 2 Passwort-Prüfungen gleichzeitig (jede braucht ~32 MB RAM).
# Verhindert, dass eine Flut von Login-Versuchen den Container zum Absturz bringt.
_HASH_SLOTS = threading.BoundedSemaphore(2)

# scrypt: N=2^15, r=8, p=1  -> ~32 MB RAM pro Versuch, bremst Brute-Force stark
_N, _R, _P = 2**15, 8, 1
_MAXMEM = 64 * 1024 * 1024


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    with _HASH_SLOTS:
        dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=_MAXMEM, dklen=32)
    b = lambda x: base64.b64encode(x).decode()
    return f"scrypt${_N}${_R}${_P}${b(salt)}${b(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, dk = stored.split("$")
        if algo != "scrypt":
            return False
        if len(password) > 256:
            return False
        with _HASH_SLOTS:
            calc = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt),
            n=int(n), r=int(r), p=int(p), maxmem=_MAXMEM, dklen=32,
        )
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except Exception:
        return False


# Damit ein unbekannter Benutzername genauso lange dauert wie ein falsches Passwort
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def password_problems(pw: str) -> str | None:
    if len(pw) < 12:
        return "Das Passwort muss mindestens 12 Zeichen lang sein."
    if len(pw) > 256:
        return "Das Passwort ist zu lang."
    return None


class LoginThrottle:
    """Max. 5 Fehlversuche pro IP in 15 Min., global max. 30 -> danach Sperre."""

    def __init__(self, per_ip: int = 5, global_max: int = 30, window: int = 900):
        self.per_ip, self.global_max, self.window = per_ip, global_max, window
        self._ip: dict[str, deque] = defaultdict(deque)
        self._all: deque = deque()
        self._lock = threading.Lock()

    def _prune(self, dq: deque, now: float) -> None:
        while dq and dq[0] < now - self.window:
            dq.popleft()

    def blocked(self, ip: str) -> int:
        """Sekunden bis zur Freigabe, 0 = nicht gesperrt."""
        now = time.time()
        with self._lock:
            dq = self._ip[ip]
            self._prune(dq, now)
            self._prune(self._all, now)
            if len(dq) >= self.per_ip:
                return int(dq[0] + self.window - now) + 1
            if len(self._all) >= self.global_max:
                return int(self._all[0] + self.window - now) + 1
            return 0

    def fail(self, ip: str) -> None:
        now = time.time()
        with self._lock:
            self._ip[ip].append(now)
            self._all.append(now)

    def success(self, ip: str) -> None:
        with self._lock:
            self._ip.pop(ip, None)

    def try_acquire(self, ip: str, global_: bool = True) -> int:
        """Prüft die Sperre UND zählt den Versuch sofort (atomar).

        So können parallel abgeschickte Versuche die Grenze nicht umgehen.
        Gibt Wartesekunden zurück (0 = Versuch erlaubt)."""
        now = time.time()
        with self._lock:
            if len(self._ip) > 5000:  # Speicher begrenzen: abgelaufene Einträge entfernen
                for k in [k for k, d in self._ip.items() if not d or d[-1] < now - self.window]:
                    del self._ip[k]
            dq = self._ip[ip]
            self._prune(dq, now)
            self._prune(self._all, now)
            if len(dq) >= self.per_ip:
                return int(dq[0] + self.window - now) + 1
            if global_ and len(self._all) >= self.global_max:
                return int(self._all[0] + self.window - now) + 1
            dq.append(now)
            if global_:
                self._all.append(now)
            return 0


throttle = LoginThrottle()


def csrf_token(session: dict) -> str:
    tok = session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        session["csrf"] = tok
    return tok


def csrf_ok(session: dict, submitted: str | None) -> bool:
    expected = session.get("csrf")
    return bool(expected and submitted and hmac.compare_digest(expected, submitted))
