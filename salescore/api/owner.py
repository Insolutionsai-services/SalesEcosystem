"""/v1/owner: the platform owner's console. Only logins listed in OWNER_EMAILS (server .env), or the X-Admin-Key, get in."""
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from ..services import admin_auth, owner
from ..core.models import User
from .deps import db, owner_only, signed_in, transaction

router = APIRouter(prefix="/v1/owner", dependencies=[Depends(owner_only)])
auth_router = APIRouter(prefix="/v1/admin-auth")


class AdminLogin(BaseModel):
    username: str
    password: str


@auth_router.post("/login")
def admin_login(body: AdminLogin, request: Request, response: Response):
    """The /admin sign-in: one username + password from the server's .env (hash only). Its own cookie, 12 hours."""
    try:
        admin_auth.check(body.username, body.password)
    except ValueError as e:
        raise HTTPException(422, str(e))
    response.set_cookie(admin_auth.COOKIE, admin_auth.make_cookie(), max_age=admin_auth.SESSION_S, httponly=True, samesite="strict",
                        secure=request.url.scheme == "https", path="/")
    return {"ok": True, "username": admin_auth.username()}


@auth_router.post("/logout")
def admin_logout(response: Response):
    response.delete_cookie(admin_auth.COOKIE, path="/")
    return {"ok": True}


class PlanChange(BaseModel):
    plan: str
    price: int | None = None


class UserChange(BaseModel):
    password: str | None = None
    disabled: bool | None = None


@router.get("/me")
def me(request: Request, user: User | None = Depends(signed_in)):
    """Who is signed in to the admin dashboard."""
    if admin_auth.valid(request.cookies.get(admin_auth.COOKIE)):
        return {"email": admin_auth.username(), "name": "Admin"}
    return {"email": user.email if user else "admin key", "name": user.name if user else "Admin"}


@router.get("/overview")
def overview(s=Depends(db)):
    return owner.overview(s)


@router.get("/customers")
def customers(s=Depends(db)):
    return owner.customers(s)


@router.get("/customers/{tid}")
def customer(tid: int, s=Depends(db)):
    with transaction(s, 404):
        return owner.customer(s, tid)


@router.put("/customers/{tid}/plan")
def change_plan(tid: int, body: PlanChange, s=Depends(db)):
    with transaction(s):
        owner.set_plan(s, tid, body.plan, body.price)
    return owner.customer(s, tid)


@router.get("/users")
def users(s=Depends(db)):
    return owner.all_users(s)


@router.put("/users/{uid}")
def change_user(uid: int, body: UserChange, s=Depends(db)):
    with transaction(s):
        owner.set_user(s, uid, body.password, body.disabled)
    return {"ok": True}


@router.delete("/users/{uid}")
def remove_user(uid: int, s=Depends(db)):
    with transaction(s):
        owner.remove_user(s, uid)
    return {"ok": True}


@router.get("/requests")
def requests(s=Depends(db)):
    return owner.requests(s)


@router.post("/requests/{rid}/{decision}")
def decide(rid: int, decision: str, s=Depends(db)):
    with transaction(s):
        if decision not in ("approve", "dismiss"):
            raise ValueError("decision must be approve or dismiss")
        return owner.decide(s, rid, decision == "approve")


@router.get("/pricing")
def pricing_view(companies: int | None = None):
    return owner.pricing_view(companies)


@router.get("/system")
def system(s=Depends(db)):
    return owner.system(s)
