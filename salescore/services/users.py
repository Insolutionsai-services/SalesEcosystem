"""Company users and sign-in. Passwords are hashed with scrypt (stdlib); sessions are random tokens stored hashed."""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select

from ..core.models import LoginToken, User
from ..core.utils import norm_email, now

ROLES = ("admin", "member")
MIN_PASSWORD = 8
SESSION_DAYS = 14
MAX_FAILS, LOCKOUT = 5, timedelta(minutes=10)
_fails: dict[str, tuple[int, datetime]] = {}  # ponytail: per-process throttle; move to the DB/Redis with several workers


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
    except ValueError:
        return False
    return hmac.compare_digest(got.hex(), digest)


_DUMMY = hash_password(secrets.token_urlsafe(12))  # unknown emails take as long as wrong passwords


def _valid_role(role: str) -> str:
    if role not in ROLES:
        raise ValueError("role must be admin (sales lead) or member (sales rep)")
    return role


def _revoke(s, user_id: int) -> None:
    """Signs the person out everywhere."""
    s.execute(delete(LoginToken).where(LoginToken.user_id == user_id))


def _valid_password(password: str) -> str:
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"passwords need at least {MIN_PASSWORD} characters")
    return password


def create_user(s, tenant_id: int, email: str, name: str, password: str, role: str = "member") -> User:
    email = norm_email(email)
    if not email or "@" not in email:
        raise ValueError("enter a valid email address")
    _valid_role(role)
    if s.scalars(select(User).where(User.email == email)).first():
        raise ValueError("that email already has an account")
    user = User(tenant_id=tenant_id, email=email, name=name.strip() or email.split("@")[0], role=role,
                password_hash=hash_password(_valid_password(password)))
    s.add(user)
    s.flush()
    return user


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def sign_in(s, email: str, password: str) -> tuple[User, str]:
    """Returns the user and a new session token. Same error for unknown email and wrong password."""
    email, at = norm_email(email) or "", now()
    fails, until = _fails.get(email, (0, at))
    if fails >= MAX_FAILS and at < until:
        raise ValueError("too many attempts; try again in a few minutes")
    user = s.scalars(select(User).where(User.email == email)).first()
    if not check_password(password, user.password_hash if user else _DUMMY) or user is None:
        _fails[email] = ((fails if at < until else 0) + 1, at + LOCKOUT)  # the window restarts after a quiet spell
        raise ValueError("wrong email or password")
    _fails.pop(email, None)
    token = secrets.token_urlsafe(32)
    s.execute(delete(LoginToken).where(LoginToken.expires_at < at))
    s.add(LoginToken(token_hash=_token_hash(token), user_id=user.id, expires_at=at + timedelta(days=SESSION_DAYS)))
    user.last_login_at = at
    return user, token


def user_for_token(s, token: str | None) -> User | None:
    if not token:
        return None
    row = s.get(LoginToken, _token_hash(token))
    if row is None or row.expires_at < now():
        return None
    return s.get(User, row.user_id)


def sign_out(s, token: str | None) -> None:
    if token:
        s.execute(delete(LoginToken).where(LoginToken.token_hash == _token_hash(token)))


def change_password(s, user: User, current: str, new: str) -> None:
    if not check_password(current, user.password_hash):
        raise ValueError("current password is wrong")
    user.password_hash = hash_password(_valid_password(new))
    _revoke(s, user.id)  # other browsers must sign in again


def list_users(s, tenant_id: int) -> list[User]:
    return list(s.scalars(select(User).where(User.tenant_id == tenant_id).order_by(User.name)))


def _admins(s, tenant_id: int) -> int:
    return s.scalar(select(func.count()).select_from(User).where(User.tenant_id == tenant_id, User.role == "admin"))


def update_user(s, user: User, role: str | None = None, password: str | None = None) -> User:
    if role:
        _valid_role(role)
        if user.role == "admin" and role != "admin" and _admins(s, user.tenant_id) == 1:
            raise ValueError("the company needs at least one sales lead")
        user.role = role
    if password:  # an admin resetting a forgotten password
        user.password_hash = hash_password(_valid_password(password))
        _revoke(s, user.id)
    return user


def remove_user(s, actor: User, user: User) -> None:
    if user.id == actor.id:
        raise ValueError("you can't remove yourself")
    if user.role == "admin" and _admins(s, user.tenant_id) == 1:
        raise ValueError("the company needs at least one sales lead")
    _revoke(s, user.id)
    s.delete(user)
