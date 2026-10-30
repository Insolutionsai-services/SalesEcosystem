"""The receipt PDF: amount in words, pagination, and sending only once an order is fully paid."""
import re

from salescore.services import contacts, quotes, receipt


def test_amount_in_words():
    assert receipt.words(23970) == "INR Twenty Three Thousand Nine Hundred Seventy Only"
    assert receipt.words(1_05_00_000) == "INR One Crore Five Lakh Only"
    assert receipt.words(0) == "INR Zero Only"
    assert receipt.words(12.5) == "INR Twelve and Fifty Paise Only"


def test_receipt_pdf(s, tenant):
    tenant.playbook = {**tenant.playbook, "company": {"name": "SAMPLE TRADERS", "gstin": "36AAAAA0000A1Z5"}}
    c = contacts.find_or_create_contact(s, tenant, phone="9876500001", name="Asha")
    q = quotes.create_quote(s, tenant, c, [{"sku": "PNT-WE-10", "qty": 2}])
    quotes.close_quote(s, tenant, q, "won")
    pdf = receipt.render(s, tenant, q)
    assert pdf.startswith(b"%PDF") and b"SAMPLE TRADERS" in pdf and b"SALES ORDER" in pdf
    assert len(re.findall(rb"/Type /Page\b(?!s)", pdf)) == 1
    q.lines = q.lines * 40  # 40 rows: the items block holds 31 per page
    assert len(re.findall(rb"/Type /Page\b(?!s)", receipt.render(s, tenant, q))) == 2


def test_scheduler_sends_receipt_for_an_order_paid_outside_the_app(s, tenant):
    from salescore.core.utils import now
    c = contacts.find_or_create_contact(s, tenant, phone="9876500002")
    q = quotes.create_quote(s, tenant, c, [{"sku": "PNT-WE-10", "qty": 1}])
    quotes.close_quote(s, tenant, q, "won")
    q.paid_amount, q.paid_at = q.total, now()  # payment written straight to the database
    assert receipt.send_due(s, tenant, now()) == 1
    assert receipt.send_due(s, tenant, now()) == 0  # once only


def test_payment_reminders_follow_the_schedule(s, tenant, monkeypatch):
    from datetime import timedelta

    from salescore.analytics import report
    from salescore.core.utils import now
    from salescore.services import compliance, dues
    monkeypatch.setattr(compliance, "in_quiet_hours", lambda *_: False)
    c = contacts.find_or_create_contact(s, tenant, phone="9876500003", name="Ravi")
    q = quotes.create_quote(s, tenant, c, [{"sku": "PNT-WE-10", "qty": 2}])
    quotes.close_quote(s, tenant, q, "won")
    quotes.record_payment(s, tenant, q, round(q.total / 4, 2))
    assert dues.remind_due(s, tenant, now()) == 0  # day 0: nothing planned yet
    row, = dues.pending(s, tenant, now())
    assert row["balance"] == round(q.total - q.paid_amount, 2) and row["reminders"] == 0
    r = report.report(s, tenant, 30)
    assert r["outstanding"] == row["balance"] and r["dues"][0]["quote_id"] == q.id
    day4 = now() + timedelta(days=4)  # first reminder at 3 days
    assert dues.remind_due(s, tenant, day4) == 1 and dues.remind_due(s, tenant, day4) == 0  # once per step
    assert dues.pending(s, tenant, day4)[0]["reminders"] == 1
    quotes.record_payment(s, tenant, q, q.total - q.paid_amount)
    assert dues.pending(s, tenant, day4) == [] and dues.remind_due(s, tenant, now() + timedelta(days=8)) == 0
