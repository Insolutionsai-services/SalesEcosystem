"""/v1 endpoints. Routes only parse, authorise and commit; all logic lives in services/ and workflows/."""
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select

from ..analytics import report
from ..analytics.metrics import analyse
from ..analytics.team import feed, roster
from ..analytics.training import train_tenant
from ..core import agents as agent_config
from ..core.config import PUBLIC_BASE_URL
from ..core.models import Activity, Campaign, Contact, Quote, Task, Tenant, User
from ..core.playbook import setting
from ..core.utils import norm_email, norm_phone, now
from ..marketing import attribution, audience, orchestrator, poster, strategy
from ..services import activity, catalog, channels, compliance, contacts, plan, quotes, receipt, tasks, users
from ..workflows import approvals, copilot, followups, inbound, photo_order, scheduler
from . import schemas
from .deps import db, manager, owned, require_admin, signed_in, tenant_of, transaction

router = APIRouter(prefix="/v1")


@router.get("/tenants", dependencies=[Depends(require_admin)])
def list_tenants(s=Depends(db)):
    """Platform owner: every company, where it came from and how far its trial is. Sign-ups from outreach carry source="mail ..."."""
    return [{"id": t.id, "name": t.name, "plan": t.plan, "since": t.plan_started_at.date().isoformat(), "source": t.source, "api_key": t.api_key,
             "members": [u.email for u in s.scalars(select(User).where(User.tenant_id == t.id))]}
            for t in s.scalars(select(Tenant).order_by(Tenant.id))]


@router.post("/tenants", dependencies=[Depends(require_admin)])
def create_tenant(body: schemas.NewTenant, s=Depends(db)):
    owner = body.model_dump(include={"owner_email", "owner_name", "owner_password"})
    tenant = Tenant(plan="unlimited", **body.model_dump(exclude=set(owner)))  # created by the platform owner: no trial caps
    with transaction(s):
        s.add(tenant)
        s.flush()
        if body.owner_email:
            users.create_user(s, tenant.id, body.owner_email, body.owner_name, body.owner_password or "", "admin")
    return {"id": tenant.id, "api_key": tenant.api_key}


@router.get("/brand")
def brand():
    """Public: the name on the sign-in page. A company's own name shows once its people are signed in."""
    return {"name": "GYC Paints & Co"}


def code_version() -> float:
    """Newest modification time of the server's Python code."""
    return max(p.stat().st_mtime for p in PACKAGE_DIR.rglob("*.py"))


PACKAGE_DIR = Path(__file__).resolve().parent.parent
STARTED_WITH = code_version()


@router.get("/me")
def me(t: Tenant = Depends(tenant_of), user: User | None = Depends(signed_in)):
    return {"id": t.id, "name": t.name, "user": schemas.user_out(user) if user else None, "channel": setting(t, "channel"), "currency": setting(t, "currency"),
            "stale": code_version() > STARTED_WITH}  # code changed since start: the UI asks for a restart


def channel_status(t: Tenant) -> dict:
    ok = channels.ready(t)
    return {"channel": setting(t, "channel"),
            "whatsapp": {"ready": ok["whatsapp"], "phone_id": t.wa_phone_id or "", "has_token": bool(t.wa_token),
                         "webhook": f"{PUBLIC_BASE_URL.rstrip('/')}/webhooks/whatsapp",
                         "missing_env": [k for k in ("WA_VERIFY_TOKEN", "WA_APP_SECRET") if not os.getenv(k)]},
            "email": {"ready": ok["email"], "from": os.getenv("SMTP_FROM") or "",
                      "missing_env": [k for k in channels.SMTP_KEYS if not os.getenv(k)]}}


@router.get("/channels")
def get_channels(t: Tenant = Depends(tenant_of)):
    """Which ways out are connected. WhatsApp details are saved per company; email comes from SMTP_* in .env."""
    return channel_status(t)


@router.put("/channels/whatsapp", dependencies=[Depends(manager)])
def set_whatsapp(body: schemas.WhatsAppSetup, t: Tenant = Depends(tenant_of), s=Depends(db)):
    with transaction(s):
        t.wa_phone_id = body.phone_id.strip() or None
        if body.token.strip():
            t.wa_token = body.token.strip()
        if not t.wa_phone_id:
            t.wa_token = None  # disconnect
    return channel_status(t)


@router.post("/channels/test")
def test_channel(body: schemas.ChannelTest, t: Tenant = Depends(tenant_of)):
    """Sends one test message straight out (no consent rules: it goes to your own number or inbox)."""
    email = norm_email(body.to) if "@" in body.to else None
    phone = None if email else norm_phone(body.to)
    if not (email or phone):
        raise HTTPException(422, "enter a phone number or an email address")
    probe = Contact(tenant_id=t.id, name="Test", phone=phone, email=email)
    try:
        used = channels.deliver(t, probe, f"Test message from {t.name}: this channel works.", channel="email" if email else "whatsapp")
    except channels.DELIVERY_ERRORS as e:
        raise HTTPException(502, f"not sent: {e}") from e
    if used == "console":
        raise HTTPException(422, f"{'Email' if email else 'WhatsApp'} isn't connected yet")
    return {"sent": used}


@router.get("/playbook")
def get_playbook(t: Tenant = Depends(tenant_of)):
    return t.playbook


@router.put("/playbook", dependencies=[Depends(manager)])
def put_playbook(playbook: dict, t: Tenant = Depends(tenant_of), s=Depends(db)):
    with transaction(s):
        t.playbook = playbook
        train_tenant(s, t)  # FAQ, intent examples and templates take effect immediately
    return playbook


def _import_contacts(s, t: Tenant, text: str) -> int:
    imported = contacts.import_contacts(s, t, text)
    if setting(t, "auto_first_touch"):
        followups.schedule_first_touch(s, t, imported)
    return len(imported)


IMPORTERS = {"catalog": catalog.import_catalog, "contacts": _import_contacts, "orders": quotes.import_orders}


@router.post("/import/{kind}")
async def import_csv(kind: str, request: Request, t: Tenant = Depends(tenant_of), s=Depends(db)):
    """Body: raw CSV. catalog: sku,name,price  contacts: phone or email  orders: phone/email,sku,qty.
    Any extra columns are kept as attributes, so every industry's data fits."""
    importer = IMPORTERS.get(kind)
    if importer is None:
        raise HTTPException(404, f"kind must be one of {sorted(IMPORTERS)}")
    text = (await request.body()).decode("utf-8-sig")
    with transaction(s):
        n = importer(s, t, text)
        s.flush()
        training = train_tenant(s, t)  # new catalog/contacts/orders -> relearn right away
    return {"imported": n, "training": training}


@router.post("/inbound")
def receive(body: schemas.Inbound, t: Tenant = Depends(tenant_of), s=Depends(db)):
    """Any channel you integrate yourself (web chat, IndiaMART, portal lead, email parser) posts here."""
    with transaction(s):
        reply = inbound.handle_inbound(s, t, body.text, body.phone, body.email, body.name, body.channel)
    return {"reply": reply}


@router.get("/catalog")
def list_catalog(t: Tenant = Depends(tenant_of), s=Depends(db)):
    return catalog.list_items(s, t.id)


def _item(s, t: Tenant, sku: str):
    item = catalog.get_items(s, t.id, [sku]).get(sku)
    if item is None:
        raise HTTPException(404, "Item not found")
    return item


def _by(user: User | None) -> str:
    return user.email if user else "API key"


@router.put("/catalog/{sku:path}/stock")
def update_stock(sku: str, body: schemas.StockChange, t: Tenant = Depends(tenant_of), user: User | None = Depends(signed_in), s=Depends(db)):
    with transaction(s):
        item = _item(s, t, sku)
        catalog.change_stock(s, t, item, body.stock, body.reason or "Edited by hand", _by(user))
    return catalog.item_dict(item)


@router.post("/catalog", dependencies=[Depends(manager)])
def add_item(body: schemas.NewItem, t: Tenant = Depends(tenant_of), user: User | None = Depends(signed_in), s=Depends(db)):
    with transaction(s):
        item = catalog.add_item(s, t, body.sku, body.name, body.price, body.stock, body.description,
                                {"hsn": body.hsn} if body.hsn else None, _by(user))
        catalog.reindex(s, t.id)  # customers can find the new item right away
    return catalog.item_dict(item)


@router.post("/catalog/delete", dependencies=[Depends(manager)])
def delete_items(body: schemas.DeleteItems, t: Tenant = Depends(tenant_of), user: User | None = Depends(signed_in), s=Depends(db)):
    with transaction(s):
        found = catalog.get_items(s, t.id, body.skus)
        for item in found.values():
            catalog.delete_item(s, t, item, _by(user))
        catalog.reindex(s, t.id)
    return {"deleted": len(found)}


@router.get("/catalog/{sku:path}/history")
def stock_history(sku: str, t: Tenant = Depends(tenant_of), s=Depends(db)):
    rows = s.scalars(select(Activity).where(Activity.tenant_id == t.id, Activity.type == "stock", Activity.data["sku"].as_string() == sku)
                     .order_by(Activity.id.desc()).limit(50))
    return [schemas.activity_out(a) for a in rows]


@router.get("/catalog/search")
def search(q: str, t: Tenant = Depends(tenant_of), s=Depends(db)):
    return catalog.search_catalog(s, t.id, q)


@router.get("/tasks")
def list_tasks(kind: str | None = None, status: str = "open", t: Tenant = Depends(tenant_of), s=Depends(db)):
    """The human work queue: approvals, handoffs, meetings, site visits, callbacks."""
    return [schemas.task_out(x) for x in tasks.list_tasks(s, t.id, status, kind)]


@router.post("/approvals/{task_id}")
def approve(task_id: int, body: schemas.Decision, t: Tenant = Depends(tenant_of), user: User | None = Depends(signed_in),
            s=Depends(db)):
    with transaction(s, error_status=409):
        return approvals.decide(s, t, owned(s, Task, task_id, t), body.approve, body.text, body.send_now,
                                by=user.name if user else "API", send_at=body.send_at)


@router.get("/contacts")
def list_contacts(t: Tenant = Depends(tenant_of), s=Depends(db)):
    return [schemas.contact_out(c) for c in contacts.list_contacts(s, t.id)]


@router.post("/outreach", dependencies=[Depends(manager)])
def log_outreach(body: schemas.OutreachMail, t: Tenant = Depends(tenant_of), s=Depends(db)):
    """The outreach script mailed a lead outside GYC, or the lead wrote back: record it here so Contacts and the timeline show it."""
    with transaction(s):
        c = contacts.find_or_create_contact(s, t, email=body.email, name=body.name, source="outreach")
        c.attrs = {**c.attrs, **{k: v for k, v in (("kind", body.kind), ("city", body.city)) if v}}
        if body.direction == "in":
            c.last_inbound_at = now()
            if compliance.is_opt_out(body.text):
                c.opted_out = True
            activity.log_activity(s, t.id, c.id, "msg_in", body.text, "email", via="outreach", subject=body.subject)
        else:
            activity.log_activity(s, t.id, c.id, "msg_out", body.subject + "\n\n" + body.text, "email", outreach=body.touch, via="outreach")
    return {"contact_id": c.id}


@router.get("/outreach")
def outreach_summary(t: Tenant = Depends(tenant_of), s=Depends(db)):
    """How the cold outreach is going: every company mailed, how many mails it got, whether it replied or asked to stop."""
    leads = s.scalars(select(Contact).where(Contact.tenant_id == t.id, Contact.source == "outreach").order_by(Contact.id)).all()
    acts = s.scalars(select(Activity).where(Activity.tenant_id == t.id, Activity.channel == "email", Activity.contact_id.in_([c.id for c in leads]))).all() if leads else []
    out = {c.id: sum(1 for a in acts if a.contact_id == c.id and a.type == "msg_out") for c in leads}
    companies = [{"id": c.id, "name": c.name, "email": c.email, "kind": c.attrs.get("kind") or "general", "city": c.attrs.get("city") or "",
                  "mails": out[c.id], "replied": bool(c.last_inbound_at) and not c.opted_out, "stopped": c.opted_out} for c in leads]
    return {"companies_mailed": len(leads), "mails_sent": sum(out.values()),
            "replied": [x["name"] for x in companies if x["replied"]], "stopped": [x["name"] for x in companies if x["stopped"]],
            "last_sent_at": max((a.at for a in acts if a.type == "msg_out"), default=None), "companies": companies}


@router.get("/contacts/{contact_id}/timeline")
def contact_timeline(contact_id: int, t: Tenant = Depends(tenant_of), s=Depends(db)):
    contact = owned(s, Contact, contact_id, t)
    return {"contact": schemas.contact_out(contact),
            "events": [schemas.activity_out(a) for a in activity.timeline(s, contact.id)]}


@router.get("/quotes")
def list_quotes(status: str | None = None, t: Tenant = Depends(tenant_of), s=Depends(db)):
    return [schemas.quote_out(q) for q in quotes.list_quotes(s, t.id, status)]


@router.get("/campaigns")
def list_campaigns(t: Tenant = Depends(tenant_of), s=Depends(db)):
    return [schemas.campaign_out(c) for c in orchestrator.list_campaigns(s, t.id)]


@router.post("/contacts/{contact_id}/reply")
def human_reply(contact_id: int, body: schemas.HumanReply, t: Tenant = Depends(tenant_of), s=Depends(db)):
    with transaction(s):
        sent = inbound.human_reply(s, t, owned(s, Contact, contact_id, t), body.text, body.resume_bot)
    return {"sent": sent}


@router.post("/contacts/{contact_id}/photo")
async def contact_photo(contact_id: int, request: Request, t: Tenant = Depends(tenant_of), s=Depends(db)):
    """Body: a picture the customer sent (PNG, JPEG or WebP, up to 5 MB). The agent reads it and replies with a quote to confirm."""
    data = await request.body()
    with transaction(s):
        reply = photo_order.handle_photo(s, t, owned(s, Contact, contact_id, t), poster.save_upload(data), data)
    return {"reply": reply}


@router.post("/quotes/{quote_id}/close")
def close_quote(quote_id: int, body: schemas.CloseQuote, t: Tenant = Depends(tenant_of), s=Depends(db)):
    with transaction(s):
        quotes.close_quote(s, t, owned(s, Quote, quote_id, t), body.status, body.reason)
    return {"ok": True}


@router.post("/quotes/{quote_id}/payment")
def quote_payment(quote_id: int, body: schemas.Payment, t: Tenant = Depends(tenant_of), s=Depends(db)):
    with transaction(s):
        q = quotes.record_payment(s, t, owned(s, Quote, quote_id, t), body.amount)
        if quotes.is_paid(q):  # fully paid: the receipt goes to the customer
            receipt.send(s, t, q)
    return schemas.quote_out(q)


@router.get("/quotes/{quote_id}/receipt")
def quote_receipt(quote_id: int, t: Tenant = Depends(tenant_of), s=Depends(db)):
    q = owned(s, Quote, quote_id, t)
    if q.status != "won":
        raise HTTPException(409, "receipts are for won orders")
    return Response(receipt.render(s, t, q), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="receipt-{q.id}.pdf"'})


@router.get("/analytics")
def analytics(days: int | None = 30, t: Tenant = Depends(tenant_of), s=Depends(db)):
    """Sales dashboard: enquiries, quotes, orders, payments for the last `days` (omit or 0 = all time) + agent results."""
    return report.report(s, t, days or None)


@router.get("/plan")
def plan_status(t: Tenant = Depends(tenant_of), s=Depends(db)):
    """Trial banner: days left, limits and usage, and where to book a demo."""
    return plan.status(s, t)


@router.post("/campaigns")
def create_campaign(body: schemas.NewCampaign, t: Tenant = Depends(tenant_of), s=Depends(db)):
    """A manager's own campaign idea: still runs Creative -> Verification -> Approval."""
    with transaction(s):
        c = orchestrator.launch_custom(s, t, body.name, body.goal, body.segment, body.channel,
                                       body.writer, body.message, body.visual)
    return schemas.campaign_out(c)


@router.get("/audience/options")
def audience_options(t: Tenant = Depends(tenant_of), s=Depends(db)):
    """Standard customer criteria with the values found in this company's data (and how many customers each)."""
    return audience.options(s, t)


@router.post("/audience/preview")
def audience_preview(body: schemas.Audience, t: Tenant = Depends(tenant_of), s=Depends(db)):
    """Live count while building an audience: matched, reachable with consent, will receive, holdout."""
    return audience.preview(s, t, body.segment)


@router.get("/campaigns/{campaign_id}/recipients")
def campaign_recipients(campaign_id: int, t: Tenant = Depends(tenant_of), s=Depends(db)):
    """Who a sent campaign went to, and how interested each person who replied is."""
    return attribution.recipients(s, t, owned(s, Campaign, campaign_id, t))


@router.post("/campaigns/{campaign_id}/poster")
def campaign_poster(campaign_id: int, body: schemas.PosterWords, t: Tenant = Depends(tenant_of), s=Depends(db)):
    """Design (or redesign) the campaign poster with the company name; optional own words."""
    with transaction(s):
        c = orchestrator.redesign_poster(s, t, owned(s, Campaign, campaign_id, t), body.headline, body.subline, body.cta)
    return schemas.campaign_out(c)


@router.post("/campaigns/{campaign_id}/image")
async def campaign_image(campaign_id: int, request: Request, t: Tenant = Depends(tenant_of), s=Depends(db)):
    """Body: the company's own poster (PNG, JPEG or WebP, up to 5 MB)."""
    data = await request.body()
    with transaction(s):
        c = orchestrator.attach_upload(s, t, owned(s, Campaign, campaign_id, t), data)
    return schemas.campaign_out(c)


@router.delete("/campaigns/{campaign_id}/image")
def campaign_image_remove(campaign_id: int, t: Tenant = Depends(tenant_of), s=Depends(db)):
    with transaction(s):
        c = orchestrator.remove_image(s, t, owned(s, Campaign, campaign_id, t))
    return schemas.campaign_out(c)


@router.get("/marketing/recommendations")
def recommendations(t: Tenant = Depends(tenant_of), s=Depends(db)):
    """Strategy agent: ranked plays (who, offer, channel, when, expected results). No LLM, no cost."""
    return [p.to_dict() for p in strategy.recommend(s, t)]


@router.post("/marketing/launch")
def launch(body: schemas.LaunchPlay, t: Tenant = Depends(tenant_of), s=Depends(db)):
    """Runs Creative -> Verification for a recommended play and queues it for approval."""
    with transaction(s):
        c = orchestrator.launch_play(s, t, body.play, body.visual)
    return schemas.campaign_out(c)


@router.post("/marketing/measure")
def measure(t: Tenant = Depends(tenant_of), s=Depends(db)):
    """Analytics agent: re-measure recent campaigns now (also runs daily)."""
    with transaction(s):
        n = attribution.refresh(s, t)
    return {"measured": n}


@router.get("/feed")
def activity_feed(limit: int = 40, t: Tenant = Depends(tenant_of), s=Depends(db)):
    """What the agents did recently, in plain words (newest first)."""
    return feed(s, t, min(limit, 200))


@router.post("/copilot")
def ask_copilot(body: schemas.CopilotMessage, t: Tenant = Depends(tenant_of), s=Depends(db)):
    """Manager copilot: plain-language questions and instructions to the agent team."""
    with transaction(s):
        return copilot.ask(s, t, body.message, body.history)


@router.get("/agents")
def list_agents(t: Tenant = Depends(tenant_of), s=Depends(db)):
    """Each agent with its on/off state, 30-day activity and editable settings."""
    team = roster(s, t)
    for a in team:
        a["fields"] = agent_config.fields_with_values(t, a["key"])
        a["locked_reason"] = agent_config.ALWAYS_ON.get(a["key"])
    return {"agents": team, "presets": [{"key": k, "label": v["label"], "on": v["on"]} for k, v in agent_config.PRESETS.items()]}


@router.put("/agents/{key}", dependencies=[Depends(manager)])
def update_agent(key: str, body: schemas.AgentUpdate, t: Tenant = Depends(tenant_of), s=Depends(db)):
    """Switch an agent on/off and/or change its settings; the local models retrain right away."""
    with transaction(s):
        t.playbook = agent_config.update(t.playbook, key, body.values, body.enabled)
        train_tenant(s, t)
    return {"key": key, "status": agent_config.state(t, key), "fields": agent_config.fields_with_values(t, key)}


@router.post("/agents/preset", dependencies=[Depends(manager)])
def apply_preset(body: schemas.Preset, t: Tenant = Depends(tenant_of), s=Depends(db)):
    """One click: run only what this company needs (e.g. replies only, campaigns only)."""
    with transaction(s):
        t.playbook = agent_config.apply_preset(t.playbook, body.preset)
    return {k: agent_config.state(t, k) for k in agent_config.FIELDS}


@router.get("/team")
def team(t: Tenant = Depends(tenant_of), s=Depends(db)):
    """The agent roster from the plan with each agent's activity."""
    return roster(s, t)


@router.post("/tick")
def tick(daily: bool = False, t: Tenant = Depends(tenant_of), s=Depends(db)):
    with transaction(s):
        return scheduler.tick(s, t, daily)


@router.post("/train", dependencies=[Depends(manager)])
def train(t: Tenant = Depends(tenant_of), s=Depends(db)):
    """Relearn this company's data now: catalog index, FAQ, intents, lead-scoring model."""
    return train_tenant(s, t)


@router.get("/insights")
def insights(t: Tenant = Depends(tenant_of), s=Depends(db)):
    with transaction(s):
        return analyse(s, t)
