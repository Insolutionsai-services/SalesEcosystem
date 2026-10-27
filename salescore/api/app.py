"""ASGI entry point: `uvicorn salescore.api.app:app`. Serves the API and the single-page UI at /."""
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response

from ..ai.llm import LLMUnavailable
from ..core.config import MEDIA_DIR, PUBLIC_BASE_URL
from ..core.models import init_db
from . import auth, routes, webhooks

UI = Path(__file__).resolve().parent.parent / "web" / "index.html"

init_db()
app = FastAPI(title="Sales Core", version="0.1")
app.include_router(routes.router)
app.include_router(auth.router)
app.include_router(webhooks.router)


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


@app.exception_handler(LLMUnavailable)
async def llm_unavailable(request: Request, exc: LLMUnavailable):
    return JSONResponse(status_code=502, content={"detail": f"LLM unavailable: {exc}"})
