"""Request bodies and response shapes."""
from pydantic import BaseModel

from ..core.models import Activity, Campaign, Contact, Quote, Task, User


class NewTenant(BaseModel):
    name: str
    playbook: dict = {}
    wa_phone_id: str | None = None
    wa_token: str | None = None
    owner_email: str | None = None     # first admin who signs in to the UI
    owner_name: str = ""
    owner_password: str | None = None


class Payment(BaseModel):
    amount: float


class Login(BaseModel):
    email: str
    password: str


class PasswordChange(BaseModel):
    current: str
    new: str


class NewUser(BaseModel):
    name: str = ""
    email: str
    password: str
    role: str = "member"


class UserEdit(BaseModel):
    role: str | None = None
    password: str | None = None        # admin reset


class WhatsAppSetup(BaseModel):
    phone_id: str = ""
    token: str = ""          # blank = keep the saved token


class ChannelTest(BaseModel):
    to: str                  # a phone number (WhatsApp) or an email address


class Inbound(BaseModel):
    text: str
    phone: str | None = None
    email: str | None = None
    name: str | None = None
    channel: str | None = "api"


class Decision(BaseModel):
    approve: bool
    text: str | None = None  # optional edited message that replaces the draft
    send_now: bool = False   # campaigns: ignore the planned send time


class HumanReply(BaseModel):
    text: str
    resume_bot: bool = False


class CloseQuote(BaseModel):
    status: str
    reason: str = ""


class NewCampaign(BaseModel):
    name: str
    goal: str = ""
    segment: dict = {}
    channel: str = "messaging"   # messaging | social
    writer: str = "ai"           # ai = Creative writes it | own = message below, sent as written
    message: str | None = None
    visual: str = "none"         # none | ai_poster | upload (upload the file next, to /campaigns/{id}/image)


class LaunchPlay(BaseModel):
    play: str
    visual: str = "none"


class PosterWords(BaseModel):
    headline: str | None = None
    subline: str | None = None
    cta: str | None = None


class Audience(BaseModel):
    segment: dict = {}


class AgentUpdate(BaseModel):
    enabled: bool | None = None
    values: dict = {}


class Preset(BaseModel):
    preset: str


class CopilotMessage(BaseModel):
    message: str
    history: list[dict] = []


def fields(obj, *names: str) -> dict:
    return {n: getattr(obj, n) for n in names}


def user_out(u: User) -> dict:
    return fields(u, "id", "name", "email", "role", "last_login_at")


def contact_out(c: Contact) -> dict:
    return fields(c, "id", "name", "phone", "email", "stage", "score", "segment", "source", "attrs",
                  "opted_out", "bot_paused", "last_inbound_at", "created_at")


def activity_out(a: Activity) -> dict:
    return fields(a, "id", "type", "channel", "body", "data", "at")


def quote_out(q: Quote) -> dict:
    return fields(q, "id", "contact_id", "lines", "subtotal", "discount_pct", "tax", "total", "status", "note",
                  "valid_until", "created_at", "closed_at", "paid_at") | {"paid_amount": q.paid_amount or 0}


def campaign_out(c: Campaign) -> dict:
    return fields(c, "id", "name", "goal", "segment", "draft", "status", "stats", "created_at", "play", "channel",
                  "plan", "variants", "image_brief", "checks", "results", "send_at", "sent_at") | {
        "image_url": f"/media/{c.image}" if c.image else None}


def task_out(t: Task) -> dict:
    return fields(t, "id", "kind", "status", "contact_id", "due_at", "data")
