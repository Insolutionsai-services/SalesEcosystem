"""Request-scoped helpers shared by every route: DB session, tenant auth, ownership checks, error mapping."""
import hmac
import os
from contextlib import contextmanager

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select

from ..core import config
from ..core.models import Session, Tenant, User
from ..services import access, admin_auth, plan, users

DOMAIN_ERRORS = (ValueError, KeyError)
SESSION_COOKIE = "sc_session"


def db():
    with Session() as s:
        yield s


def signed_in(request: Request, s=Depends(db)) -> User | None:
    user = users.user_for_token(s, request.cookies.get(SESSION_COOKIE))
    if s.dirty:
        s.commit()  # keeps the slid idle timeout even on read-only requests; at most one write a minute
    return user


def current_user(user: User | None = Depends(signed_in)) -> User:
    if user is None:
        raise HTTPException(401, "please sign in")
    return user


def _lead_only() -> HTTPException:
    return HTTPException(403, "only the sales lead can do this")


def admin_user(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise _lead_only()
    return user


def tenant_of(request: Request, x_api_key: str | None = Header(None), user: User | None = Depends(signed_in), s=Depends(db)) -> Tenant:
    """Every /v1 call is scoped to one company: a signed-in user's, or the owner of the X-API-Key (integrations).
    A company whose trial has ended stays readable; anything that changes data opens the demo screen."""
    if x_api_key:
        tenant = s.scalars(select(Tenant).where(Tenant.api_key == x_api_key)).first()
        if tenant is None:
            raise HTTPException(401, "invalid api key")
    elif user is None:
        raise HTTPException(401, "please sign in")
    else:
        tenant = s.get(Tenant, user.tenant_id)
        if (page := access.blocked_page(user, request.method, request.url.path)) is not None:
            raise HTTPException(403, f"your sales lead hasn't given you access to {dict((k, l) for k, l, _ in access.PAGES)[page]}")
    if request.method != "GET" and plan.expired(tenant):
        raise plan.PlanLimit("trial_over")
    return tenant


def manager(x_api_key: str | None = Header(None), user: User | None = Depends(signed_in)) -> None:
    """Settings that change what customers get (limits, agents, channels): the sales lead, or the API key holder."""
    if not x_api_key and (user is None or user.role != "admin"):
        raise _lead_only()


def agent_editor(key: str, x_api_key: str | None = Header(None), user: User | None = Depends(signed_in)) -> None:
    """An agent's rules: the sales lead, the API key holder, or a rep the lead gave edit access to this agent."""
    if not x_api_key and access.agent_level(user, key) != "edit":
        raise HTTPException(403, "your sales lead hasn't let you change this agent")


def owner_only(request: Request, x_admin_key: str | None = Header(None), user: User | None = Depends(signed_in)) -> None:
    """The platform owner: signed in at /admin (admin username + password), a login in OWNER_EMAILS, or the admin key."""
    if admin_auth.valid(request.cookies.get(admin_auth.COOKIE)):
        return
    if user is not None and user.email in config.OWNER_EMAILS:
        return
    expected = os.getenv("ADMIN_KEY")
    if x_admin_key and expected and hmac.compare_digest(x_admin_key, expected):
        return
    if user is None and not x_admin_key:
        raise HTTPException(401, "please sign in")
    raise HTTPException(403, "only the platform owner can open this")


def require_admin(request: Request, x_admin_key: str | None = Header(None)) -> None:
    """Platform-owner calls: signed in at /admin, or (only when ADMIN_KEY is set in .env) the X-Admin-Key header.
    Leaving ADMIN_KEY empty switches the key off entirely: the /admin sign-in is then the only way in."""
    if admin_auth.valid(request.cookies.get(admin_auth.COOKIE)):
        return
    expected = os.getenv("ADMIN_KEY")
    if x_admin_key and expected and hmac.compare_digest(x_admin_key, expected):
        return
    raise HTTPException(401, "sign in at /admin" if not x_admin_key else "invalid admin key")


def owned(s, model, id_: int, tenant: Tenant):
    """Fetch a row by id, 404 unless it belongs to this tenant (never leak another tenant's data)."""
    obj = s.get(model, id_)
    if obj is None or obj.tenant_id != tenant.id:
        raise HTTPException(404, f"{model.__name__} not found")
    return obj


@contextmanager
def transaction(s, error_status: int = 422):
    """Commit on success; business-rule errors roll back and become an HTTP error."""
    try:
        yield
    except DOMAIN_ERRORS as e:
        s.rollback()
        raise HTTPException(error_status, str(e))
    s.commit()
