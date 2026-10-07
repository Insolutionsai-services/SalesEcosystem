"""WhatsApp Cloud API webhook: one endpoint for all tenants, routed by phone_number_id."""
import hashlib
import hmac
import json
import logging
import os

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select

from ..core.models import Session, Tenant
from ..workflows.inbound import handle_inbound

log = logging.getLogger("salescore.webhooks")
router = APIRouter(prefix="/webhooks")


def valid_signature(raw: bytes, header: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def message_text(m: dict) -> str:
    return (m.get("text", {}).get("body") or m.get("button", {}).get("text")
            or m.get("interactive", {}).get("button_reply", {}).get("title")
            or f"[customer sent a {m.get('type')} message]")


@router.get("/whatsapp", response_class=PlainTextResponse)
def verify(mode: str = Query(alias="hub.mode"), token: str = Query(alias="hub.verify_token"),
           challenge: str = Query(alias="hub.challenge")):
    expected = os.getenv("WA_VERIFY_TOKEN")
    if mode == "subscribe" and expected and hmac.compare_digest(token, expected):
        return challenge
    raise HTTPException(403)


@router.post("/whatsapp")
async def receive(request: Request, bg: BackgroundTasks, x_hub_signature_256: str = Header("")):
    secret = os.getenv("WA_APP_SECRET")
    if not secret:
        raise HTTPException(503, "WA_APP_SECRET not configured")
    raw = await request.body()
    if not valid_signature(raw, x_hub_signature_256, secret):
        raise HTTPException(401, "bad signature")
    bg.add_task(process, json.loads(raw))  # ack fast; Meta retries slow webhooks
    return {"ok": True}


def process(payload: dict) -> None:
    with Session() as s:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                phone_id = value.get("metadata", {}).get("phone_number_id")
                tenant = s.scalars(select(Tenant).where(Tenant.wa_phone_id == phone_id)).first()
                if tenant is None:
                    continue
                names = {c.get("wa_id"): c.get("profile", {}).get("name") for c in value.get("contacts", [])}
                for m in value.get("messages", []):
                    try:
                        handle_inbound(s, tenant, message_text(m), phone=m["from"], name=names.get(m["from"]),
                                       channel="whatsapp", ext_id=m.get("id"))
                        s.commit()
                    except Exception:  # one bad message must not drop the rest of the batch
                        s.rollback()
                        log.exception("whatsapp message %s failed", m.get("id"))
