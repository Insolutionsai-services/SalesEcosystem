from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from salescore.core.models import Task
from salescore.core.playbook import setting
from salescore.core.utils import hour_in_window, norm_phone
from salescore.services import catalog, compliance, contacts, quotes
from salescore.services.activity import log_activity


def test_utils_and_playbook_defaults(tenant):
    assert norm_phone("98765 43210") == "919876543210" and norm_phone("+91-98765-43210") == "919876543210"
    assert hour_in_window(22, 21, 9) and hour_in_window(3, 21, 9) and not hour_in_window(12, 21, 9)
    assert setting(tenant, "pricing.tax_pct") == 18          # tenant value
    assert setting(tenant, "pricing.validity_days") == 7     # platform default
    assert setting(tenant, "currency") == "INR"


def test_quote_prices_from_catalog_with_limits(s, tenant):
    c = contacts.find_or_create_contact(s, tenant, phone="9876543210")
    q = quotes.create_quote(s, tenant, c, [{"sku": "PNT-WE-10", "qty": 3}, {"sku": "PVC-EL-2", "qty": 10}], discount_pct=5)
    assert q.subtotal == 7550 and q.status == "sent"
    assert q.tax == round(7550 * 0.95 * 0.18, 2) and q.total == round(7550 * 0.95 * 1.18, 2)

    over_auto = quotes.create_quote(s, tenant, c, [{"sku": "PNT-WE-10", "qty": 1}], discount_pct=8)
    short = quotes.create_quote(s, tenant, c, [{"sku": "IOT-GATE-M", "qty": 6}])
    assert over_auto.status == short.status == "pending_approval"
    assert "in stock 4" in short.note
    assert len(s.scalars(select(Task).where(Task.kind == "approval")).all()) == 2
    with pytest.raises(ValueError):
        quotes.create_quote(s, tenant, c, [{"sku": "PNT-WE-10", "qty": 1}], discount_pct=15)
    with pytest.raises(ValueError):
        quotes.create_quote(s, tenant, c, [{"sku": "NOPE", "qty": 1}])

    tenant.playbook = {**tenant.playbook, "pricing": {**tenant.playbook["pricing"], "auto_approve_max_total": 5000}}
    big = quotes.create_quote(s, tenant, c, [{"sku": "PNT-WE-10", "qty": 3}])  # within discount and stock, but over 5,000
    assert big.status == "pending_approval" and "auto-send limit" in big.note


def test_contact_dedupe_and_merge(s, tenant):
    a = contacts.find_or_create_contact(s, tenant, phone="9000000009", name="Ravi")
    b = contacts.find_or_create_contact(s, tenant, phone="+91 90000 00009", email="RAVI@x.com")
    assert a is b and b.email == "ravi@x.com" and b.name == "Ravi"


def test_catalog_search_across_industries(s, tenant):
    assert catalog.search_catalog(s, tenant.id, "2 inch pvc elbow")[0]["sku"] == "PVC-EL-2"
    assert catalog.search_catalog(s, tenant.id, "east facing plot")[0]["sku"] == "PLOT-A12"
    assert catalog.search_catalog(s, tenant.id, "white emulsion 10 litre")[0]["sku"] == "PNT-WE-10"


def test_compliance_gate(s, tenant):
    c = contacts.find_or_create_contact(s, tenant, phone="9000000001")
    at = datetime(2026, 9, 30, 8, 0)  # 13:30 IST
    assert compliance.check(s, tenant, c, "marketing", at) == "no marketing consent"
    assert compliance.check(s, tenant, c, "followup", at) == "no consent and never enquired"
    c.last_inbound_at = at - timedelta(hours=2)
    assert compliance.check(s, tenant, c, "reply", at) is None
    contacts.record_consent(s, tenant, c, True, "form")
    s.flush()
    assert compliance.check(s, tenant, c, "marketing", at) is None
    for _ in range(2):
        log_activity(s, tenant.id, c.id, "msg_out", body="x", kind="marketing")
    s.flush()
    assert compliance.check(s, tenant, c, "marketing", at) == "frequency cap"
    tenant.playbook = {**tenant.playbook, "compliance": {"quiet_hours": [21, 9]}}
    assert compliance.check(s, tenant, c, "followup", datetime(2026, 9, 30, 17, 0)) == "quiet hours"  # 22:30 IST
    c.opted_out = True
    assert compliance.check(s, tenant, c, "reply", at) == "opted out"
    assert compliance.is_opt_out(" STOP ") and not compliance.is_opt_out("stop sending samples, send the quote")


def test_payments_and_sales_report(s, tenant):
    from salescore.analytics.report import report
    c = contacts.find_or_create_contact(s, tenant, phone="9876500001")
    q = quotes.create_quote(s, tenant, c, [{"sku": "PNT-WE-10", "qty": 2}])
    with pytest.raises(ValueError, match="won orders"):
        quotes.record_payment(s, tenant, q, 100)
    quotes.close_quote(s, tenant, q, "won")
    quotes.record_payment(s, tenant, q, 1000)
    with pytest.raises(ValueError, match="still due"):
        quotes.record_payment(s, tenant, q, q.total)
    r = report(s, tenant, 30)
    assert r["current"]["quotes"] == 1 and r["current"]["orders"] == 1 and r["current"]["paid"] == 1000
    assert r["outstanding"] == round(q.total - 1000, 2) and r["unpaid_orders"] == 1
    assert r["current"]["enquiries"] == 1 and sum(r["trend"]["orders"]) == 1
