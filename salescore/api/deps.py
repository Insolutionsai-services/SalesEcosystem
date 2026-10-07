"""Request-scoped helpers shared by every route: DB session, tenant auth, ownership checks, error mapping."""
import hmac
import os
from contextlib import contextmanager

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select

from ..core.models import Session, Tenant, User
from ..services import users

DOMAIN_ERRORS = (ValueError, KeyError)
SESSION_COOKIE = "sc_session"


def db():
    with Session() as s:
        yield s


def signed_in(request: Request, s=Depends(db)) -> User | None:
    return users.user_for_token(s, request.cookies.get(SESSION_COOKIE))


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


def tenant_of(x_api_key: str | None = Header(None), user: User | None = Depends(signed_in), s=Depends(db)) -> Tenant:
    """Every /v1 call is scoped to one company: a signed-in user's, or the owner of the X-API-Key (integrations)."""
    if x_api_key:
        tenant = s.scalars(select(Tenant).where(Tenant.api_key == x_api_key)).first()
        if tenant is None:
            raise HTTPException(401, "invalid api key")
        return tenant
    if user is None:
        raise HTTPException(401, "please sign in")
    return s.get(Tenant, user.tenant_id)


def manager(x_api_key: str | None = Header(None), user: User | None = Depends(signed_in)) -> None:
    """Settings that change what customers get (limits, agents, channels): the sales lead, or the API key holder."""
    if not x_api_key and (user is None or user.role != "admin"):
        raise _lead_only()


def require_admin(x_admin_key: str = Header(...)) -> None:
    expected = os.getenv("ADMIN_KEY")
    if not expected:
        raise HTTPException(503, "ADMIN_KEY not configured")
    if not hmac.compare_digest(x_admin_key, expected):
        raise HTTPException(401, "invalid admin key")


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
