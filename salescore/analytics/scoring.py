"""Pure ML/statistics on DataFrames; no database access, so each function is testable on its own.
Frames: contacts(id, stage, created_at, opted_out, last_inbound_at, consent),
        acts(contact_id, type, at, kind), quotes(id, contact_id, status, total, created_at)."""
import math

import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

MIN_TRAINING_ROWS = 40
LOST_STATUSES = ("lost", "expired")


def percentile_score(x: pd.Series, higher_is_better: bool = True) -> pd.Series:
    """Percentile rank -> 1..5. Stays meaningful with 3 customers or 300k."""
    return (x.rank(method="average", pct=True, ascending=higher_is_better) * 5).apply(math.ceil)


def rfm_segments(quotes: pd.DataFrame, today) -> dict[int, str]:
    """RFM on won orders -> champions / loyal / new / at_risk / hibernating / regular."""
    won = quotes[quotes.status == "won"]
    if won.empty:
        return {}
    g = won.groupby("contact_id").agg(last=("created_at", "max"), orders=("id", "count"))
    days = (today - g["last"]).dt.days
    r, f = percentile_score(days, higher_is_better=False), percentile_score(g.orders)
    seg = pd.Series("regular", index=g.index)
    seg[(f >= 4) & (r >= 3)] = "loyal"
    seg[(r >= 4) & (f >= 4)] = "champions"
    seg[(g.orders == 1) & (r >= 4)] = "new"
    seg[(r <= 2) & (f >= 3)] = "at_risk"
    seg[(r <= 1) & (f <= 2)] = "hibernating"
    return seg.to_dict()


def lead_features(contacts: pd.DataFrame, acts: pd.DataFrame, quotes: pd.DataFrame, today) -> pd.DataFrame:
    c = contacts.set_index("id")
    X = c[["consent"]].astype(float)
    for t in ("msg_in", "msg_out"):
        X[t] = acts[acts.type == t].groupby("contact_id").size().reindex(X.index, fill_value=0)
    X["quotes"] = quotes.groupby("contact_id").size().reindex(X.index, fill_value=0)
    X["max_quote"] = quotes.groupby("contact_id")["total"].max().reindex(X.index)
    X["days_since_inbound"] = (today - c["last_inbound_at"]).dt.days
    X["age_days"] = (today - c["created_at"]).dt.days
    return X


def outcome_labels(contacts: pd.DataFrame, quotes: pd.DataFrame) -> tuple[set, set]:
    """(won contact ids, lost contact ids) from quote outcomes and the 'lost' stage."""
    won = set(quotes.loc[quotes.status == "won", "contact_id"])
    lost = set(quotes.loc[quotes.status.isin(LOST_STATUSES), "contact_id"]) | set(contacts.loc[contacts.stage == "lost", "id"])
    return won, lost - won


def fit_lead_model(contacts: pd.DataFrame, acts: pd.DataFrame, quotes: pd.DataFrame, today):
    """Gradient boosting on won/lost history. None until there are enough labelled outcomes of both kinds."""
    if contacts.empty:
        return None
    X = lead_features(contacts, acts, quotes, today)
    won, lost = outcome_labels(contacts, quotes)
    labelled = X.loc[X.index.isin(won | lost)]
    y = labelled.index.isin(won)
    if len(labelled) < MIN_TRAINING_ROWS or not 0 < y.sum() < len(y):
        return None
    return HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05).fit(labelled, y)


def heuristic_scores(X: pd.DataFrame):
    # ponytail: hand-weighted cold-start score; the trained model replaces it once history exists
    recency = 1 / (1 + X["days_since_inbound"].fillna(60) / 7)
    return (0.15 + 0.35 * recency + 0.1 * X["msg_in"].clip(upper=5) / 5 + 0.25 * (X["quotes"] > 0)
            + 0.15 * X["consent"]).clip(0, 0.95).to_numpy()


def predict_scores(model, contacts: pd.DataFrame, acts: pd.DataFrame, quotes: pd.DataFrame, today) -> dict[int, float]:
    """P(win) per contact from a fitted model, or the heuristic when model is None."""
    if contacts.empty:
        return {}
    X = lead_features(contacts, acts, quotes, today)
    p = model.predict_proba(X)[:, 1] if model is not None else heuristic_scores(X)
    return dict(zip(X.index, map(float, p)))


def score_leads(contacts: pd.DataFrame, acts: pd.DataFrame, quotes: pd.DataFrame, today) -> dict[int, float]:
    """Fit + predict in one go (batch use)."""
    return predict_scores(fit_lead_model(contacts, acts, quotes, today), contacts, acts, quotes, today)


def reorder_due(quotes: pd.DataFrame, today) -> list[int]:
    """Contacts with 2+ orders whose usual (median) gap between orders has passed."""
    won = quotes[quotes.status == "won"].sort_values("created_at")
    due = []
    for cid, g in won.groupby("contact_id"):
        if len(g) < 2:
            continue
        gap = g.created_at.diff().dt.days.median()
        if gap and (today - g.created_at.iloc[-1]).days >= gap:
            due.append(int(cid))
    return due


def speed_to_lead_minutes(acts: pd.DataFrame) -> float | None:
    """Median minutes from a customer message to our next message."""
    msgs = acts[acts.type.isin(["msg_in", "msg_out"])].sort_values("at")
    gaps = []
    for _, g in msgs.groupby("contact_id"):
        pending = None
        for row in g.itertuples():
            if row.type == "msg_in" and pending is None:
                pending = row.at
            elif row.type == "msg_out" and pending is not None:
                gaps.append((row.at - pending).total_seconds() / 60)
                pending = None
    return round(float(pd.Series(gaps).median()), 1) if gaps else None
