"""One schema for every industry: fixed core columns + JSON `attrs` for whatever a tenant's catalog/leads carry."""
import secrets
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    inspect,
)
from sqlalchemy import text as sql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import DATABASE_URL
from .utils import now


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    """A client company. `playbook` is its whole sales configuration (see core/playbook.py)."""
    __tablename__ = "tenants"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    api_key: Mapped[str] = mapped_column(String(64), unique=True, default=lambda: secrets.token_urlsafe(32))
    playbook: Mapped[dict] = mapped_column(JSON, default=dict)
    wa_phone_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    wa_token: Mapped[str | None] = mapped_column(Text)  # ponytail: plaintext; encrypt (KMS/pgcrypto) before real clients


class User(Base):
    """A person at the client company who signs in: admin = sales lead (everything), member = sales rep (daily work)."""
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    email: Mapped[str] = mapped_column(String(200), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), default="member")
    password_hash: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)


class LoginToken(Base):
    """A signed-in browser. Only the SHA-256 of the cookie value is stored, so a database leak can't sign anyone in."""
    __tablename__ = "login_tokens"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class Contact(Base):
    __tablename__ = "contacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(32), index=True)
    email: Mapped[str | None] = mapped_column(String(200), index=True)
    stage: Mapped[str] = mapped_column(String(50), default="new")
    score: Mapped[float | None] = mapped_column(Float)
    segment: Mapped[str | None] = mapped_column(String(50))
    source: Mapped[str | None] = mapped_column(String(100))
    attrs: Mapped[dict] = mapped_column(JSON, default=dict)
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False)
    bot_paused: Mapped[bool] = mapped_column(Boolean, default=False)  # a human owns the conversation
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Consent(Base):
    """Append-only ledger; the latest row per contact decides (DPDP: provable, withdrawable)."""
    __tablename__ = "consents"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"), index=True)
    purpose: Mapped[str] = mapped_column(String(50), default="marketing")
    granted: Mapped[bool] = mapped_column(Boolean)
    source: Mapped[str | None] = mapped_column(String(200))
    at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Item(Base):
    """Anything a tenant sells: SKU, plot, service, plan. Price comes from the tenant's own catalog."""
    __tablename__ = "items"
    __table_args__ = (UniqueConstraint("tenant_id", "sku"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    sku: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    price: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), default="INR")
    stock: Mapped[float | None] = mapped_column(Float)  # None = not stock-tracked (services, plots use attrs.status)
    attrs: Mapped[dict] = mapped_column(JSON, default=dict)


class Quote(Base):
    """Quotes and orders are one table: an order is a quote with status 'won'."""
    __tablename__ = "quotes"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"), index=True)
    lines: Mapped[list] = mapped_column(JSON)
    subtotal: Mapped[float] = mapped_column(Float)
    discount_pct: Mapped[float] = mapped_column(Float, default=0)
    tax: Mapped[float] = mapped_column(Float, default=0)
    total: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(30))  # pending_approval | sent | won | lost | expired
    note: Mapped[str | None] = mapped_column(Text)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)    # when it was marked won / lost
    paid_amount: Mapped[float | None] = mapped_column(Float)        # money received so far (orders)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)      # latest payment


class Task(Base):
    """The durable work queue: follow-ups, meetings, approvals, human hand-offs, reorder nudges."""
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30), index=True)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    due_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Activity(Base):
    """Event log: every message, quote, block, stage change. Analytics and ML read only this + quotes."""
    __tablename__ = "activities"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"), index=True)
    type: Mapped[str] = mapped_column(String(30), index=True)  # msg_in | msg_out | blocked | quote | stage | insights
    channel: Mapped[str | None] = mapped_column(String(20))
    body: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)


class Campaign(Base):
    __tablename__ = "campaigns"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    goal: Mapped[str] = mapped_column(Text)
    segment: Mapped[dict] = mapped_column(JSON, default=dict)
    draft: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="pending_approval")
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    # the marketing chain: Strategy -> Creative -> Verification -> Approval -> Distribution -> Analytics
    play: Mapped[str | None] = mapped_column(String(40))       # strategy play key, e.g. win_back
    channel: Mapped[str | None] = mapped_column(String(20))    # messaging | social
    plan: Mapped[dict | None] = mapped_column(JSON)            # Strategy agent output (why, offer, expected)
    variants: Mapped[list | None] = mapped_column(JSON)        # Creative agent output: [{label, angle, text}]
    image_brief: Mapped[str | None] = mapped_column(Text)
    image: Mapped[str | None] = mapped_column(String(80))         # poster file name under MEDIA_DIR
    checks: Mapped[dict | None] = mapped_column(JSON)          # Verification agent output: {ok, errors, warnings}
    results: Mapped[dict | None] = mapped_column(JSON)         # Analytics agent output (attribution, A/B, lift)
    send_at: Mapped[datetime | None] = mapped_column(DateTime)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


engine = create_engine(DATABASE_URL)
Session = sessionmaker(engine, expire_on_commit=False)


def init_db(eng=engine):
    Base.metadata.create_all(eng)
    add_missing_columns(eng)


def add_missing_columns(eng) -> None:
    """Adds new nullable columns to existing tables so old databases keep working.
    ponytail: additive-only; switch to Alembic migrations before production."""
    insp = inspect(eng)
    with eng.begin() as conn:
        for table in Base.metadata.sorted_tables:
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in have:
                    conn.execute(sql(f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(eng.dialect)}"))
