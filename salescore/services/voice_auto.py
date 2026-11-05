"""Phones the contact list on a schedule: one customer at a time, only inside the calling window, with a gap after each call.
Runs from `python -m salescore tick` (cron) and, under `make dev`, from a loop every minute. Off until Voice settings say auto-call is on."""
import logging
import time
from datetime import datetime, timedelta

from sqlalchemy import func, select

from ..core import agents
from ..core.models import Activity, Contact, Session, Tenant
from ..core.playbook import setting
from ..core.utils import norm_phone, now, to_local
from . import compliance, plan
from .contacts import update_contact

LAWFUL = (9 * 60, 21 * 60)   # no calls outside 09:00-21:00 local time, whatever the window says (India's telemarketing rules)


def business(contact: Contact) -> str:
    """What the customer's business is, for the opening: the CSV's business column (or business_type / company), else their segment."""
    a = contact.attrs or {}
    return next((str(v) for k in ("business", "business_type", "company") if (v := a.get(k))), contact.segment or "")


def budget(t: Tenant) -> int:
    if plan.is_trial(t):   # the free minutes run on the platform's keys: the company's own setting cannot raise them
        return plan.LIMITS["call_minutes"]
    try:
        return max(int(float(setting(t, "voice.minutes_budget") or 0)), 0)
    except ValueError:
        return 0


def max_call_min(t: Tenant) -> float:
    try:
        return max(float(setting(t, "voice.max_call_min") or 3), 0.5)
    except ValueError:
        return 3.0


def minutes_used(s, t: Tenant) -> int:
    """Minutes the phone company bills: every call that connected, rounded UP to the whole minute. Unanswered calls cost nothing."""
    return plan.used(s, t, "call_minutes")


def call_limit(s, t: Tenant):
    """How long the next call may last. On the free plan one long call can never pass the minutes left."""
    return min(max_call_min(t), budget(t) - minutes_used(s, t)) if plan.is_trial(t) else max_call_min(t)


def out_of_minutes(s, t: Tenant) -> str | None:
    """Why no call may be made, or None. A call needs room for a full-length call, so the budget is never overspent."""
    used = minutes_used(s, t)
    left = budget(t) - used
    if plan.is_trial(t):
        return "Your trial has ended." if plan.expired(t) else None if left > 0 else f"You have used your {budget(t)} free test call minutes."
    if left < max_call_min(t):   # a budget of 0 means no calls, never unlimited
        return f"Phone minutes used up: {used} of {budget(t)} used, {max(left, 0)} left (a call can last up to {max_call_min(t):g}). Raise 'Phone minutes you have' in Voice settings after you top up."
    return None


def _minutes(hhmm, default: int) -> int:
    try:
        h, m = str(hhmm).split(":")
        return int(h) * 60 + int(m)
    except ValueError:
        return default


def lawful_hour(t: Tenant, at: datetime) -> bool:
    local = to_local(at, setting(t, "timezone"))
    return LAWFUL[0] <= local.hour * 60 + local.minute < LAWFUL[1]


def in_window(t: Tenant, at: datetime) -> bool:
    local = to_local(at, setting(t, "timezone"))
    lo, hi = max(_minutes(setting(t, "voice.call_from"), 600), LAWFUL[0]), min(_minutes(setting(t, "voice.call_until"), 1080), LAWFUL[1])
    return lo <= local.hour * 60 + local.minute < hi


def next_contact(s, t: Tenant, at: datetime) -> tuple[Contact | None, str]:
    """The next customer to call and why not otherwise. Needs: Voice agent on, auto-call on, inside the window, the gap since the last
    call over, and a customer with recorded consent, not opted out, never dialled before."""
    if not agents.enabled(t, "voice") or setting(t, "voice.auto_call") != "on":
        return None, "auto-calling is off"
    if plan.is_trial(t):
        return None, "real phone calls are set up by our team"
    if not in_window(t, at):
        return None, "outside the calling window"
    if (why := out_of_minutes(s, t)):
        return None, why
    try:
        gap = timedelta(minutes=max(int(setting(t, "voice.gap_min") or 5), 1))
    except ValueError:
        gap = timedelta(minutes=5)
    people = s.scalars(select(Contact).where(Contact.tenant_id == t.id, Contact.opted_out.is_(False), Contact.phone.is_not(None))
                       .order_by(Contact.id)).all()
    last = max([x for x in (s.scalar(select(func.max(Activity.at)).where(Activity.tenant_id == t.id, Activity.type == "call")),
                            *[datetime.fromisoformat(c.attrs["voice_tried_at"]) for c in people if (c.attrs or {}).get("voice_tried_at")]) if x], default=None)
    if last and at - last < gap:
        return None, "waiting out the gap since the last call"
    ok = compliance.consenting_ids(s, t.id)
    return next(((c, "ok") for c in people if c.id in ok and not (c.attrs or {}).get("voice_tried_at")), (None, "nobody left to call"))


def run(s, t: Tenant) -> dict:
    """Dial the next customer if everything allows it."""
    at = now()
    c, why = next_contact(s, t, at)
    if not c:
        return {"called": None, "why": why}
    from fastapi import HTTPException

    from ..api.routes import _call_script, _voice   # imported here: the api layer imports this package
    try:
        if _voice("GET", "/status", timeout=3).get("busy"):
            return {"called": None, "why": "a call is in progress"}
        phone = norm_phone(c.phone, setting(t, "country_code"))
        _voice("POST", "/dial", timeout=60, json={"to": "+" + phone, "config": {**_call_script(t, s), "customer_business": business(c)}})
    except HTTPException as e:
        if e.status_code == 502:   # the phone provider refused this number: note it, do not try again every minute
            update_contact(s, t, c, attrs={"voice_tried_at": at.isoformat(), "voice_error": str(e.detail)[:120]})
        return {"called": None, "why": str(e.detail)}
    update_contact(s, t, c, attrs={"voice_tried_at": at.isoformat()})
    return {"called": c.phone}


def loop(every: int = 60) -> None:
    """For `make dev`, which has no cron: look every minute."""
    while True:
        time.sleep(every)
        try:
            with Session() as s:
                for tid in s.scalars(select(Tenant.id)).all():
                    run(s, s.get(Tenant, tid))
                    s.commit()
        except Exception:
            logging.exception("voice auto-call loop")
