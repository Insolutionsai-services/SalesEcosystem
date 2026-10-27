"""Sign in / out, self-service sign-up, email codes, Google sign-in, and the company's team of users."""
import os
import secrets

import httpx
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse

from ..core.config import PUBLIC_BASE_URL
from ..core.models import Tenant, User
from ..services import otp, users
from . import schemas
from .deps import SESSION_COOKIE, admin_user, current_user, db, owned, transaction

router = APIRouter(prefix="/v1")
GOOGLE = "https://accounts.google.com/o/oauth2/v2/auth"


def _google_on() -> bool:
    return bool(os.getenv("GOOGLE_CLIENT_ID") and os.getenv("GOOGLE_CLIENT_SECRET"))


def _remember(request: Request, response: Response, token: str) -> None:
    # HttpOnly: page scripts can't read it; Strict: other sites can't make the browser send it
    response.set_cookie(SESSION_COOKIE, token, max_age=users.SESSION_DAYS * 86400, httponly=True, samesite="strict",
                        secure=request.url.scheme == "https", path="/")


@router.get("/auth/options")
def auth_options():
    """Public: which sign-in choices the page should offer."""
    return {"google": _google_on(), "email_codes": otp.configured()}


@router.post("/auth/login")
def login(body: schemas.Login, request: Request, response: Response, s=Depends(db)):
    with transaction(s):  # 422, not 401: a wrong password is a form error, not a signed-out session
        user = users.check_login(s, body.email, body.password)
        if otp.required_for(user.email):  # right password: now prove the inbox too
            otp.send(s, user.email, "login")
            return {"otp": True, "email": user.email}
        token = users.open_session(s, user)
    _remember(request, response, token)
    return schemas.user_out(user)


@router.post("/auth/signup")
def sign_up(body: schemas.SignUp, request: Request, response: Response, s=Depends(db)):
    """With email codes set up: check the details, mail a code, create the account when it's entered.
    Without them: create the account straight away (the email is not verified)."""
    with transaction(s):
        if len(body.password) < users.MIN_PASSWORD:
            raise ValueError(f"passwords need at least {users.MIN_PASSWORD} characters")
        if "@" not in body.email or users.find(s, body.email) or otp.is_reserved(body.email):
            raise ValueError("enter a valid email address that has no account yet")
        data = {"name": body.name, "company": body.company, "password_hash": users.hash_password(body.password)}
        if otp.configured():
            otp.send(s, body.email, "signup", data)
            return {"otp": True, "email": body.email.strip().lower()}
        otp.throttle_signups()
        user = users.create_workspace(s, data["company"], body.email, data["name"], data["password_hash"])
        token = users.open_session(s, user)
    _remember(request, response, token)
    return schemas.user_out(user)


@router.post("/auth/verify")
def verify_code(body: schemas.VerifyCode, request: Request, response: Response, s=Depends(db)):
    with transaction(s):
        purpose, data = otp.verify(s, body.email, body.code)
        if purpose == "signup":
            user = users.create_workspace(s, data["company"], body.email, data["name"], data["password_hash"])
        else:
            user = users.find(s, body.email)
            if user is None:
                raise ValueError("that account no longer exists")
        token = users.open_session(s, user)
    _remember(request, response, token)
    return schemas.user_out(user)


@router.get("/auth/google")
def google_start(request: Request):
    if not _google_on():
        return RedirectResponse("/?login_error=google_off")
    state = secrets.token_urlsafe(24)
    url = httpx.URL(GOOGLE, params={"client_id": os.environ["GOOGLE_CLIENT_ID"], "response_type": "code", "scope": "openid email profile",
                                    "redirect_uri": f"{PUBLIC_BASE_URL.rstrip('/')}/v1/auth/google/callback", "state": state, "prompt": "select_account"})
    r = RedirectResponse(str(url))
    # Lax, not Strict: the browser comes back from google.com, which Strict would not send the cookie for
    r.set_cookie("sc_oauth", state, max_age=600, httponly=True, samesite="lax", secure=request.url.scheme == "https", path="/")
    return r


@router.get("/auth/google/callback")
def google_callback(request: Request, code: str = "", state: str = "", s=Depends(db)):
    fail = RedirectResponse("/?login_error=google")
    fail.delete_cookie("sc_oauth", path="/")
    if not code or not state or state != request.cookies.get("sc_oauth") or not _google_on():
        return fail
    try:
        tok = httpx.post("https://oauth2.googleapis.com/token", timeout=20, data={
            "code": code, "client_id": os.environ["GOOGLE_CLIENT_ID"], "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
            "redirect_uri": f"{PUBLIC_BASE_URL.rstrip('/')}/v1/auth/google/callback", "grant_type": "authorization_code"})
        tok.raise_for_status()
        info = httpx.get("https://openidconnect.googleapis.com/v1/userinfo", timeout=20,
                         headers={"Authorization": f"Bearer {tok.json()['access_token']}"})
        info.raise_for_status()
        info = info.json()
    except (httpx.HTTPError, KeyError, ValueError):
        return fail
    if not info.get("email") or not info.get("email_verified"):
        return fail
    with transaction(s):  # Google has already proven the inbox, so no code is needed
        user = users.find(s, info["email"]) or users.create_workspace(
            s, "", info["email"], info.get("name", ""), users.hash_password(secrets.token_urlsafe(24)))
        token = users.open_session(s, user)
    ok = RedirectResponse("/")
    ok.delete_cookie("sc_oauth", path="/")
    _remember(request, ok, token)
    return ok


@router.post("/auth/logout")
def logout(request: Request, response: Response, s=Depends(db)):
    with transaction(s):
        users.sign_out(s, request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.put("/auth/password")
def change_password(body: schemas.PasswordChange, user: User = Depends(current_user), s=Depends(db)):
    with transaction(s):
        users.change_password(s, user, body.current, body.new)
    return {"ok": True}


@router.get("/users")
def team_users(user: User = Depends(current_user), s=Depends(db)):
    return [schemas.user_out(u) for u in users.list_users(s, user.tenant_id)]


@router.post("/users")
def add_user(body: schemas.NewUser, admin: User = Depends(admin_user), s=Depends(db)):
    with transaction(s):
        u = users.create_user(s, admin.tenant_id, body.email, body.name, body.password, body.role)
    return schemas.user_out(u)


@router.put("/users/{user_id}")
def edit_user(user_id: int, body: schemas.UserEdit, admin: User = Depends(admin_user), s=Depends(db)):
    with transaction(s):
        u = users.update_user(s, owned(s, User, user_id, s.get(Tenant, admin.tenant_id)), body.role, body.password)
    return schemas.user_out(u)


@router.delete("/users/{user_id}")
def delete_user(user_id: int, admin: User = Depends(admin_user), s=Depends(db)):
    with transaction(s):
        users.remove_user(s, admin, owned(s, User, user_id, s.get(Tenant, admin.tenant_id)))
    return {"ok": True}
