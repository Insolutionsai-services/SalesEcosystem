from datetime import datetime, timedelta

import pandas as pd

from salescore.analytics import scoring
from salescore.analytics.metrics import ACT_COLS, QUOTE_COLS

TODAY = datetime(2026, 9, 30)


def orders(history: dict[int, list[str]]) -> pd.DataFrame:
    rows = [{"contact_id": cid, "status": "won", "total": 1000.0, "created_at": pd.Timestamp(d)}
            for cid, dates in history.items() for d in dates]
    return pd.DataFrame([{"id": i, **r} for i, r in enumerate(rows)], columns=QUOTE_COLS)


def test_rfm_and_reorder():
    quotes = orders({1: ["2026-06-01", "2026-07-01", "2026-08-01", "2026-09-25"],  # frequent + recent
                     2: ["2025-10-01", "2025-11-01", "2025-12-01"],                # used to buy often, stopped
                     3: ["2026-09-20"]})                                           # brand new buyer
    seg = scoring.rfm_segments(quotes, TODAY)
    assert seg[1] in ("champions", "loyal") and seg[2] in ("at_risk", "hibernating") and seg[3] == "new"
    assert scoring.reorder_due(quotes, TODAY) == [2]  # 1 bought 5 days ago; 2 is ~10 months overdue


def test_cold_start_scoring_prefers_recent_enquiry():
    contacts = pd.DataFrame([{"id": i, "stage": "new", "created_at": TODAY - timedelta(days=10), "opted_out": False,
                              "last_inbound_at": TODAY - timedelta(days=d), "consent": True} for i, d in [(10, 1), (11, 60)]])
    scores = scoring.score_leads(contacts, pd.DataFrame(columns=ACT_COLS), pd.DataFrame(columns=QUOTE_COLS), TODAY)
    assert scores[10] > scores[11]


def test_speed_to_lead():
    acts = pd.DataFrame([{"contact_id": 1, "type": "msg_in", "at": TODAY},
                         {"contact_id": 1, "type": "msg_out", "at": TODAY + timedelta(minutes=2)}], columns=ACT_COLS)
    assert scoring.speed_to_lead_minutes(acts) == 2.0
