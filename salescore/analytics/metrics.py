"""Loads a tenant's data into frames, refreshes ML fields on contacts, returns the manager's metrics snapshot."""
from datetime import timedelta

import pandas as pd
from sqlalchemy import select

from ..core.models import Activity, Contact, Quote, Tenant
from ..core.utils import now
from ..ml import store
from ..services import compliance, tasks
from . import scoring

CONTACT_COLS = ["id", "stage", "created_at", "opted_out", "last_inbound_at", "consent"]
ACT_COLS = ["contact_id", "type", "at", "kind"]
QUOTE_COLS = ["id", "contact_id", "status", "total", "created_at"]
HOT_LEAD_MIN = 0.6
DEFAULT_WIN_PROB = 0.3


def _frame(rows: list[dict], columns: list[str], date_cols: tuple[str, ...]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=columns)
    for col in date_cols:  # all-None columns would otherwise be object dtype
        df[col] = pd.to_datetime(df[col])
    return df


def load_frames(s, tenant_id: int, contact_id: int | None = None) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """All of a tenant's contacts, activities and quotes as frames; or just one contact's (real-time scoring)."""
    def scoped(model, fk):
        q = select(model).where(model.tenant_id == tenant_id)
        return s.scalars(q.where(fk == contact_id) if contact_id is not None else q)

    contacts = _frame([{"id": c.id, "stage": c.stage, "created_at": c.created_at, "opted_out": c.opted_out,
                        "last_inbound_at": c.last_inbound_at, "consent": compliance.has_consent(s, c)}
                       for c in scoped(Contact, Contact.id)], CONTACT_COLS, ("created_at", "last_inbound_at"))
    acts = _frame([{"contact_id": a.contact_id, "type": a.type, "at": a.at, "kind": a.data.get("kind")}
                   for a in scoped(Activity, Activity.contact_id)], ACT_COLS, ("at",))
    quotes = _frame([{"id": q.id, "contact_id": q.contact_id, "status": q.status, "total": q.total,
                      "created_at": q.created_at} for q in scoped(Quote, Quote.contact_id)], QUOTE_COLS, ("created_at",))
    return contacts, acts, quotes


def lead_model(tenant_id: int):
    brain = store.load(tenant_id)
    return brain.lead_model if brain else None


def score_contact(s, tenant: Tenant, contact: Contact) -> float:
    """Real-time P(win) for one contact with the tenant's trained model (milliseconds, no LLM)."""
    frames = load_frames(s, tenant.id, contact.id)
    contact.score = round(scoring.predict_scores(lead_model(tenant.id), *frames, now()).get(contact.id, 0.0), 3)
    return contact.score


def ai_usage(s, tenant_id: int, since) -> dict:
    """How many replies were answered by the local models vs the LLM, and what the LLM cost."""
    rows = s.execute(select(Activity.type, Activity.data).where(Activity.tenant_id == tenant_id, Activity.at >= since,
                                                               Activity.type.in_(["llm", "msg_out"]))).all()
    llm = [d for t, d in rows if t == "llm"]
    replies = [d.get("via") for t, d in rows if t == "msg_out" and d.get("kind") == "reply"]
    local = sum(v == "fastpath" for v in replies)
    return {
        "replies": len(replies), "replies_local": local,
        "local_answer_rate": round(local / len(replies), 3) if replies else None,
        "llm_calls": len(llm),
        "llm_tokens": sum(d.get("tokens_in", 0) + d.get("tokens_out", 0) for d in llm),
        "llm_cost_usd": round(sum(d.get("cost_usd", 0) for d in llm), 6),
        "llm_avg_seconds": round(sum(d.get("seconds", 0) for d in llm) / len(llm), 2) if llm else None,
    }


def refresh_contacts(s, tenant_id: int, segments: dict, scores: dict) -> None:
    for c in s.scalars(select(Contact).where(Contact.tenant_id == tenant_id)):
        c.segment, c.score = segments.get(c.id, c.segment), round(scores.get(c.id, c.score or 0), 3)


def schedule_reorders(s, tenant_id: int, due_ids: list[int]) -> None:
    for cid in set(due_ids) - tasks.open_contact_ids(s, tenant_id, "reorder"):
        tasks.add_task(s, tenant_id, cid, "reorder")


def analyse(s, tenant: Tenant) -> dict:
    """Refreshes segment/score on contacts, schedules reorder nudges, returns the metrics snapshot."""
    today = now()
    contacts, acts, quotes = load_frames(s, tenant.id)
    segments = scoring.rfm_segments(quotes, today)
    scores = scoring.predict_scores(lead_model(tenant.id), contacts, acts, quotes, today)
    refresh_contacts(s, tenant.id, segments, scores)
    schedule_reorders(s, tenant.id, scoring.reorder_due(quotes, today))

    last30 = acts[acts["at"] >= today - timedelta(days=30)]
    decided = quotes[quotes.status.isin(("won", *scoring.LOST_STATUSES))]
    open_quotes = quotes[quotes.status == "sent"]
    won_ids, _ = scoring.outcome_labels(contacts, quotes)
    closed_ids = won_ids | set(contacts.loc[contacts.opted_out.astype(bool) | (contacts.stage == "lost"), "id"])
    return {
        "contacts_by_stage": contacts.groupby("stage").size().to_dict(),
        "segments": pd.Series(segments, dtype=object).value_counts().to_dict(),
        "messages_30d": last30.type.value_counts().to_dict(),
        "blocked_sends_30d": int((last30.type == "blocked").sum()),
        "opted_out": int(contacts.opted_out.astype(bool).sum()),
        "median_first_response_min": scoring.speed_to_lead_minutes(acts),
        "quotes": quotes.status.value_counts().to_dict(),
        "win_rate": round(float((decided.status == "won").mean()), 3) if len(decided) else None,
        "revenue_won_30d": float(quotes[(quotes.status == "won") & (quotes.created_at >= today - timedelta(days=30))].total.sum()),
        "open_pipeline": float(open_quotes.total.sum()),
        "forecast_weighted_pipeline": round(float(sum(r.total * scores.get(r.contact_id, DEFAULT_WIN_PROB)
                                                      for r in open_quotes.itertuples())), 2),
        "hot_leads": sorted(((cid, round(p, 2)) for cid, p in scores.items()
                             if p >= HOT_LEAD_MIN and cid not in closed_ids), key=lambda x: -x[1])[:10],
        "ai_30d": ai_usage(s, tenant.id, today - timedelta(days=30)),
    }
