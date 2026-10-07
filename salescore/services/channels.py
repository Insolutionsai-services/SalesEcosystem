"""Outbound channels. `send` is the only way anything leaves the system: gate -> deliver -> log."""
import logging
import mimetypes
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

import httpx

from ..core.config import MEDIA_DIR, WA_API_VERSION
from ..core.models import Contact, Tenant
from ..core.playbook import setting
from ..core.utils import now
from . import compliance
from .activity import log_activity

log = logging.getLogger("salescore.channels")
DELIVERY_ERRORS = (httpx.HTTPError, smtplib.SMTPException, OSError, RuntimeError, KeyError)


CAPTION_LIMIT = 1024
_wa_media: dict[tuple[str, str], str] = {}  # (phone id, file) -> uploaded media id; each poster uploads once


def _image(name: str) -> tuple[Path, str]:
    path = Path(MEDIA_DIR) / name
    return path, mimetypes.guess_type(path.name)[0] or "image/png"


def _wa_media_id(tenant: Tenant, name: str) -> str:
    # ponytail: cache lives in memory; a restart re-uploads once per poster
    key = (tenant.wa_phone_id, name)
    if key not in _wa_media:
        path, mime = _image(name)
        r = httpx.post(f"https://graph.facebook.com/{WA_API_VERSION}/{tenant.wa_phone_id}/media",
                       headers={"Authorization": f"Bearer {tenant.wa_token}"},
                       data={"messaging_product": "whatsapp", "type": mime},
                       files={"file": (path.name, path.read_bytes(), mime)}, timeout=60)
        r.raise_for_status()
        _wa_media[key] = r.json()["id"]
    return _wa_media[key]


def _whatsapp(tenant: Tenant, contact: Contact, text: str, image: str | None = None) -> None:
    if compliance.in_service_window(contact, now()):
        body = ({"type": "image", "image": {"id": _wa_media_id(tenant, image), "caption": text[:CAPTION_LIMIT]}}
                if image else {"type": "text", "text": {"body": text}})
    else:  # outside the 24h window Meta only allows approved templates
        template = setting(tenant, "whatsapp_template") or {}
        if not template.get("name"):
            raise RuntimeError("outside 24h window: set playbook.whatsapp_template {name, language}")
        components = [{"type": "body", "parameters": [{"type": "text", "text": text}]}]
        if image and template.get("image"):  # the approved template has an image header: the poster goes there
            components.insert(0, {"type": "header", "parameters": [{"type": "image", "image": {"id": _wa_media_id(tenant, image)}}]})
        body = {"type": "template", "template": {
            "name": template["name"], "language": {"code": template.get("language", "en")}, "components": components}}
    httpx.post(f"https://graph.facebook.com/{WA_API_VERSION}/{tenant.wa_phone_id}/messages",
               headers={"Authorization": f"Bearer {tenant.wa_token}"},
               json={"messaging_product": "whatsapp", "to": contact.phone, **body}, timeout=20).raise_for_status()


def _email(tenant: Tenant, contact: Contact, text: str, image: str | None = None) -> None:
    # ponytail: one SMTP account from env; per-tenant sender domains when clients need their own
    msg = EmailMessage()
    msg["From"], msg["To"] = os.environ["SMTP_FROM"], contact.email
    msg["Subject"] = setting(tenant, "email_subject") or tenant.name
    msg.set_content(text)
    if image:
        path, mime = _image(image)
        main, sub = mime.split("/")
        msg.add_attachment(path.read_bytes(), maintype=main, subtype=sub, filename=path.name)
    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.getenv("SMTP_PORT", "587"))) as smtp:
        smtp.starttls()
        smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        smtp.send_message(msg)


SMTP_KEYS = ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM")


def ready(tenant: Tenant) -> dict[str, bool]:
    """Which real channels have their details filled in."""
    return {"whatsapp": bool(tenant.wa_phone_id and tenant.wa_token), "email": all(os.getenv(k) for k in SMTP_KEYS)}


def pick(tenant: Tenant, contact: Contact, channel: str | None = None) -> str:
    """auto = WhatsApp if connected and the customer has a phone, else email, else console (shown in the app only)."""
    channel, ok = channel or setting(tenant, "channel"), ready(tenant)
    if channel in ("auto", "whatsapp") and contact.phone and ok["whatsapp"]:
        return "whatsapp"
    if channel in ("auto", "email") and contact.email and ok["email"]:
        return "email"
    return "console"


def deliver(tenant: Tenant, contact: Contact, text: str, image: str | None = None, channel: str | None = None) -> str:
    """Pushes text (and an optional poster) over the tenant's channel with no checks. Returns the channel used."""
    channel = pick(tenant, contact, channel)
    if send_via := {"whatsapp": _whatsapp, "email": _email}.get(channel):
        send_via(tenant, contact, text, image)
        return channel
    log.info("[console -> %s]%s %s", contact.phone or contact.email, f" [image {image}]" if image else "", text)
    return "console"


def send(s, tenant: Tenant, contact: Contact, text: str, kind: str, via: str | None = None, image: str | None = None,
         **meta) -> bool:
    """kind: reply | followup | marketing (see compliance.check); via: fastpath | llm | human | campaign.
    meta (e.g. campaign_id, variant) is logged with the message so analytics can attribute results."""
    reason = compliance.check(s, tenant, contact, kind, now())
    if reason:
        log_activity(s, tenant.id, contact.id, "blocked", body=text, kind=kind, reason=reason, **meta)
        return False
    if kind == "marketing":
        text += "\n\n" + setting(tenant, "opt_out_footer")
    return send_unchecked(s, tenant, contact, text, kind, via, image, **meta)


def send_unchecked(s, tenant: Tenant, contact: Contact, text: str, kind: str, via: str | None = None,
                   image: str | None = None, **meta) -> bool:
    """Bypasses the gate. Only for messages the law requires, e.g. confirming an opt-out."""
    try:
        channel = deliver(tenant, contact, text, image)
    except DELIVERY_ERRORS as e:
        # ponytail: failed sends are logged, not retried; add a retry task when a channel proves flaky
        log_activity(s, tenant.id, contact.id, "failed", body=text, kind=kind, error=str(e), **meta)
        return False
    log_activity(s, tenant.id, contact.id, "msg_out", body=text, channel=channel, kind=kind, via=via,
                 **({"image": image} if image else {}), **meta)
    return True
