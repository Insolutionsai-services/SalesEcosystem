"""Sign in / out and the company's team of users."""
from fastapi import APIRouter, Depends, Request, Response

from ..core.models import Tenant, User
from ..services import users
from . import schemas
from .deps import SESSION_COOKIE, admin_user, current_user, db, owned, transaction

router = APIRouter(prefix="/v1")


@router.post("/auth/login")
def login(body: schemas.Login, request: Request, response: Response, s=Depends(db)):
    with transaction(s):  # 422, not 401: a wrong password is a form error, not a signed-out session
        user, token = users.sign_in(s, body.email, body.password)
    # HttpOnly: page scripts can't read it; Strict: other sites can't make the browser send it
    response.set_cookie(SESSION_COOKIE, token, max_age=users.SESSION_DAYS * 86400, httponly=True, samesite="strict",
                        secure=request.url.scheme == "https", path="/")
    return schemas.user_out(user)


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
