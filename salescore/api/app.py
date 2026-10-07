"""ASGI entry point: `uvicorn salescore.api.app:app`. Serves the API and the single-page UI at /."""
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from ..ai.llm import LLMUnavailable
from ..core.config import MEDIA_DIR
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


MEDIA_NAME = re.compile(r"^[A-Za-z0-9_-]{16}\.(png|jpg|webp)$")


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
