"""ASGI entry point: `uvicorn salescore.api.app:app`. Serves the API and the single-page UI at /."""
import re
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response

from ..ai.llm import LLMUnavailable
from ..services.plan import PlanLimit
from ..core.config import CONTACT_URL, DEMO_BOOKING_URL, MEDIA_DIR, PUBLIC_BASE_URL
from ..core.models import init_db
from . import auth, routes, webhooks

UI = Path(__file__).resolve().parent.parent / "web" / "index.html"

init_db()
app = FastAPI(title="GYC", version="0.1")
app.include_router(routes.router)
app.include_router(auth.router)
app.include_router(webhooks.router)

CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src 'self' data: blob:; connect-src 'self'; "
       "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
# ponytail: 'unsafe-inline' because the UI is one inline page; move the script to a file and drop it for a strict CSP


@app.middleware("http")
async def guard(request: Request, call_next):
    """Blocks cross-site writes, and sets the browser security headers on every response."""
    origin = request.headers.get("origin")
    hosts = {request.headers.get("host"), request.headers.get("x-forwarded-host")}  # a browser can't set X-Forwarded-Host cross-site without a preflight we never allow
    if request.method not in ("GET", "HEAD", "OPTIONS") and origin and urlsplit(origin).netloc not in hosts:
        return JSONResponse({"detail": "cross-site request blocked"}, status_code=403)  # webhooks and API-key calls send no Origin
    r = await call_next(request)
    h = r.headers
    h["X-Content-Type-Options"] = "nosniff"
    h["X-Frame-Options"] = "DENY"
    h["Referrer-Policy"] = "strict-origin-when-cross-origin"
    h["Permissions-Policy"] = "camera=(self), microphone=(), geolocation=(), payment=()"
    h["Content-Security-Policy"] = CSP
    if request.url.scheme == "https":
        h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.url.path.startswith("/v1/") or request.url.path == "/":
        h["Cache-Control"] = "no-store"  # customer data never sits in the browser cache (the back button after sign-out shows nothing)
    return r


@app.get("/", include_in_schema=False)
def ui():
    return FileResponse(UI)


@app.get("/robots.txt", include_in_schema=False)
def robots():
    base = PUBLIC_BASE_URL.rstrip("/")
    return PlainTextResponse(f"User-agent: *\nAllow: /$\nDisallow: /v1/\nDisallow: /webhooks/\nDisallow: /media/\n\nSitemap: {base}/sitemap.xml\n")


@app.get("/sitemap.xml", include_in_schema=False)
def sitemap():
    loc = PUBLIC_BASE_URL.rstrip("/") + "/"
    return Response(f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>{loc}</loc></url></urlset>',
                    media_type="application/xml")


MEDIA_NAME =re.compile(r"^[A-Za-z0-9_-]{16}\.(png|jpg|webp)$")


@app.get("/media/{name}", include_in_schema=False)
def media(name: str):
    """Campaign posters. Names are random and unguessable, so chats and social tools can load them without a login."""
    path = Path(MEDIA_DIR) / name
    if not MEDIA_NAME.match(name) or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.exception_handler(PlanLimit)
async def plan_limit(request: Request, exc: PlanLimit):
    """402: the UI shows the trial screen (book a demo / contact us) instead of an error."""
    return JSONResponse(status_code=402, content={"detail": {"limit": exc.kind, "message": str(exc),
                                                             "demo_url": DEMO_BOOKING_URL, "contact_url": CONTACT_URL}})


@app.exception_handler(LLMUnavailable)
async def llm_unavailable(request: Request, exc: LLMUnavailable):
    return JSONResponse(status_code=502, content={"detail": f"LLM unavailable: {exc}"})
