"""Trial plan: self-created companies get a 14-day preview with small lifetime caps; after that the company is read-only.
Companies created by the platform owner (admin API, demo seed, `salescore plan NAME unlimited`) have no limits."""
import math
from datetime import timedelta

from sqlalchemy import select

from ..core.config import CONTACT_URL, DEMO_BOOKING_URL
from ..core.models import Activity, Campaign, Tenant, User
from ..core.utils import count, norm_email, now

TRIAL_DAYS = 14
LIMITS = {"campaigns": 2, "emails": 3, "ai": 30, "members": 2, "call_minutes": 15}  # lifetime totals for the company
LABELS = {"campaigns": "campaigns", "emails": "emails", "ai": "AI messages", "members": "team members", "call_minutes": "free test call minutes"}
PRACTICE_TURN_S = 20   # one practice exchange counts as this much call time: 45 exchanges use up 15 minutes


class PlanLimit(Exception):
    """A trial cap was reached (kind) or the trial is over ("trial_over"). The API answers 402; the UI shows the demo screen."""

    def __init__(self, kind: str):
        self.kind = kind
        super().__init__("your trial has ended" if kind == "trial_over" else "real phone calls are set up by our team, please contact us" if kind == "phone_calls"
                         else "reaching customers is set up by our team, please contact us" if kind == "public_messaging"
                         else f"trial limit reached: {LABELS[kind]} ({LIMITS[kind]})")


def is_trial(tenant: Tenant) -> bool:
    return tenant.plan == "trial"


def own_address(tenant: Tenant, email: str | None) -> bool:
    """A free company can reach one address: the one it proved at sign-up. Team members' addresses were never proved, so they don't count."""
    return not is_trial(tenant) or bool(tenant.owner_email and norm_email(email) == tenant.owner_email)


def days_left(tenant: Tenant) -> int | None:
    if not is_trial(tenant):
        return None
    left = tenant.plan_started_at + timedelta(days=TRIAL_DAYS) - now()
    return max(0, math.ceil(left.total_seconds() / 86400))  # day 1 shows 14, the last day shows 1


def expired(tenant: Tenant) -> bool:
    return is_trial(tenant) and days_left(tenant) == 0


def used(s, tenant: Tenant, kind: str) -> int:
    t = tenant.id
    if kind == "campaigns":
        return count(s, Campaign, Campaign.tenant_id == t)
    if kind == "emails":
        return count(s, Activity, Activity.tenant_id == t, Activity.type == "msg_out", Activity.channel == "email")
    if kind == "ai":
        return count(s, Activity, Activity.tenant_id == t, Activity.type == "llm")
    if kind == "call_minutes":   # what the phone company bills, each connected call rounded UP to the minute, plus practice time
        rows = s.execute(select(Activity.type, Activity.data["duration_s"].as_integer())   # only the number: call records also hold whole transcripts
                         .where(Activity.tenant_id == t, Activity.type.in_(("call", "voice_turn")))).all()
        practice = sum(k == "voice_turn" for k, _ in rows) * PRACTICE_TURN_S if is_trial(tenant) else 0   # free minutes don't come off a paid budget after an upgrade
        return sum(-(-(d or 0) // 60) for k, d in rows if k == "call") + practice // 60
    return count(s, User, User.tenant_id == t)


def status(s, tenant: Tenant) -> dict:
    """What the banner and the demo screen show."""
    trial = is_trial(tenant)
    return {"trial": trial, "own_email": tenant.owner_email if trial else None, "days_left": days_left(tenant), "expired": expired(tenant),
            "limits": LIMITS if trial else {}, "labels": LABELS, "usage": {k: used(s, tenant, k) for k in LIMITS} if trial else {},
            "demo_url": DEMO_BOOKING_URL, "contact_url": CONTACT_URL}


def check(s, tenant: Tenant, kind: str) -> None:
    """Raise PlanLimit when a trial company is out of days or has used up `kind`. Unlimited companies always pass."""
    if not is_trial(tenant):
        return
    if expired(tenant):
        raise PlanLimit("trial_over")
    if used(s, tenant, kind) >= LIMITS[kind]:
        raise PlanLimit(kind)


def set_plan(tenant: Tenant, plan: str) -> None:
    tenant.plan = plan
    if plan == "trial":
        tenant.plan_started_at = now()  # a fresh 14 days
