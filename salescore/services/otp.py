"""Email one-time codes: a 6-digit code mailed to someone proves they own the address (sign-in and sign-up).
Uses the same SMTP_* settings as outbound customer email; without them, codes are off and plain sign-in keeps working."""
import hashlib
import hmac
import os
import secrets
import smtplib
from datetime import timedelta
from email.message import EmailMessage

from sqlalchemy import delete, select

from ..core.models import EmailCode
from ..core.utils import norm_email, now

TTL, MAX_TRIES, COOLDOWN_S, PER_HOUR = timedelta(minutes=10), 5, 30, 5
SMTP_KEYS = ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM")


def configured() -> bool:
    return all(os.getenv(k) for k in SMTP_KEYS)


def required_for(email: str) -> bool:
    """Codes protect every sign-in once email is set up, except the shared demo account (it has no real inbox)."""
    exempt = {norm_email(e) for e in os.getenv("OTP_EXEMPT", "sales@demo.in").split(",")}
    return configured() and norm_email(email) not in exempt


def is_reserved(email: str) -> bool:
    """Demo and exempt addresses can't be claimed through sign-up."""
    e = norm_email(email) or ""
    return e.endswith("@demo.in") or e in {norm_email(x) for x in os.getenv("OTP_EXEMPT", "sales@demo.in").split(",")}


_signups: list = []  # ponytail: per-process; with several workers move it to the DB


def throttle_signups(limit: int = int(os.getenv("SIGNUPS_PER_HOUR", "20"))) -> None:
    """Sign-ups without an email check are capped, so nobody can mass-create workspaces."""
    at = now()
    _signups[:] = [t for t in _signups if t > at - timedelta(hours=1)]
    if len(_signups) >= limit:
        raise ValueError("too many sign-ups right now; try again later or contact us")
    _signups.append(at)


def _hash(email: str, code: str) -> str:
    return hashlib.sha256(f"{email}:{code}:{os.getenv('OTP_SECRET', '')}".encode()).hexdigest()


def _mail(to: str, code: str) -> None:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = os.environ["SMTP_FROM"], to, f"Your sign-in code: {code}"
    msg.set_content(f"Your verification code is {code}.\nIt expires in 10 minutes. If you didn't ask for it, ignore this email.")
    try:
        with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.getenv("SMTP_PORT", "587")), timeout=20) as smtp:
            smtp.starttls()
            smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        raise ValueError("we couldn't send the code email; please try again in a moment") from e


def send(s, email: str, purpose: str, data: dict | None = None) -> None:
    email, at = norm_email(email), now()
    s.execute(delete(EmailCode).where(EmailCode.created_at < at - timedelta(hours=1)))
    recent = s.scalars(select(EmailCode).where(EmailCode.email == email).order_by(EmailCode.created_at.desc())).all()
    if recent and (at - recent[0].created_at).total_seconds() < COOLDOWN_S:
        raise ValueError("wait a few seconds before asking for another code")
    if len(recent) >= PER_HOUR:
        raise ValueError("too many codes requested; try again in an hour")
    code = f"{secrets.randbelow(10**6):06d}"
    s.add(EmailCode(email=email, purpose=purpose, code_hash=_hash(email, code), data=data, expires_at=at + TTL))
    s.flush()
    _mail(email, code)  # if this fails the request rolls back, so no unusable code is left behind


def verify(s, email: str, code: str) -> tuple[str, dict | None]:
    """Returns (purpose, data) for the newest code if it matches; the code is then used up."""
    email = norm_email(email) or ""
    row = s.scalars(select(EmailCode).where(EmailCode.email == email).order_by(EmailCode.created_at.desc())).first()
    if row is None or row.expires_at < now():
        raise ValueError("that code has expired; ask for a new one")
    if row.attempts >= MAX_TRIES:
        raise ValueError("too many wrong codes; ask for a new one")
    if not hmac.compare_digest(row.code_hash, _hash(email, (code or "").strip())):
        row.attempts += 1
        s.commit()  # the count must survive the error that follows
        raise ValueError("wrong code")
    s.delete(row)
    return row.purpose, row.data
