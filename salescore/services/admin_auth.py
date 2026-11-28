"""Sign-in for the /admin dashboard: one username and password, separate from every customer login.
The server keeps only ADMIN_PASSWORD_HASH (scrypt) in .env, set with `python -m salescore admin-password`; the password itself is never stored.
A session is a signed, expiring cookie: changing the password (or ADMIN_KEY) signs every admin browser out."""
import hashlib
import hmac
import os
import re
import time

from ..core.utils import now
from . import users

COOKIE = "gyc_admin"
SESSION_S = 12 * 3600
MIN_LEN = 10
_fails: dict[str, tuple[int, object]] = {}   # ponytail: per-process throttle, like customer sign-in


def username() -> str:
    return (os.getenv("ADMIN_USERNAME") or "admin").strip().lower()


def _hash() -> str:
    return os.getenv("ADMIN_PASSWORD_HASH", "").strip()


def configured() -> bool:
    return bool(_hash())


def strong_enough(password: str) -> str | None:
    """Why a password is too weak, or None when it's fine."""
    if len(password) < MIN_LEN:
        return f"use at least {MIN_LEN} characters"
    missing = [name for name, rx in (("a capital letter", r"[A-Z]"), ("a small letter", r"[a-z]"), ("a digit", r"\d"), ("a symbol", r"[^A-Za-z0-9]"))
               if not re.search(rx, password)]
    return f"add {', '.join(missing)}" if missing else None


def check(name: str, password: str) -> None:
    """Raises ValueError unless the username and password are right. Five wrong tries lock it for 10 minutes."""
    at = now()
    fails, until = _fails.get("admin", (0, at))
    if fails >= users.MAX_FAILS and at < until:
        raise ValueError("too many attempts; try again in a few minutes")
    pw_ok = users.check_password(password or "", _hash() or users._DUMMY)   # always one scrypt: same time either way
    name_ok = hmac.compare_digest((name or "").strip().lower(), username())
    if not (configured() and name_ok and pw_ok):
        _fails["admin"] = ((fails if at < until else 0) + 1, at + users.LOCKOUT)
        raise ValueError("wrong username or password" if configured() else "the admin password isn't set on this server yet")
    _fails.pop("admin", None)


def _key() -> bytes:
    return hashlib.sha256(("gyc-admin|" + _hash() + "|" + os.getenv("ADMIN_KEY", "")).encode()).digest()


def make_cookie() -> str:
    exp = str(int(time.time()) + SESSION_S)
    return exp + "." + hmac.new(_key(), exp.encode(), hashlib.sha256).hexdigest()


def valid(cookie: str | None) -> bool:
    if not cookie or not configured() or "." not in cookie:
        return False
    exp, sig = cookie.split(".", 1)
    good = hmac.new(_key(), exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good) and exp.isdigit() and int(exp) > time.time()

