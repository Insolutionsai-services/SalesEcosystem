"""PDF receipt for a paid order, in the company's sales-order format: the labels are fixed, only the values change.
Values come from the quote (what was ordered), the contact (buyer) and playbook.company (seller)."""
import functools
import io
import logging
from datetime import datetime, timedelta
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from sqlalchemy import select

from ..core.models import Activity, Contact, Quote, Tenant
from ..core.playbook import setting
from . import channels
from .catalog import get_items
from .quotes import PAID_SLACK, is_paid

log = logging.getLogger("salescore.receipt")
L, R, TOP, ROW = 25, 570, 842, 12.5
COLS = [30, 215, 62, 50, 62, 46, 80]  # S.No, description, HSN, qty, price, disc %, amount
ITEMS_TOP, ITEMS_END, ROWS_PER_PAGE = 228, 628, 31
# ponytail: first installed font that has the rupee sign; falls back to "Rs." with the built-in font
FONTS = [("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
         ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
         ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf")]


@functools.cache
def _fonts() -> tuple[str, str, str]:
    for reg, bold in FONTS:
        if Path(reg).exists() and Path(bold).exists():
            for name, path in (("Rc", reg), ("Rc-Bold", bold)):
                if name not in pdfmetrics.getRegisteredFontNames():
                    pdfmetrics.registerFont(TTFont(name, path))
            return "Rc", "Rc-Bold", "₹"
    return "Helvetica", "Helvetica-Bold", "Rs."


ONES = ("Zero One Two Three Four Five Six Seven Eight Nine Ten Eleven Twelve Thirteen Fourteen Fifteen Sixteen "
        "Seventeen Eighteen Nineteen").split()
TENS = "_ _ Twenty Thirty Forty Fifty Sixty Seventy Eighty Ninety".split()


def _below_100(n: int) -> str:
    return ONES[n] if n < 20 else TENS[n // 10] + (" " + ONES[n % 10] if n % 10 else "")


def _below_1000(n: int) -> str:
    h, r = divmod(n, 100)
    return " ".join(filter(None, [f"{ONES[h]} Hundred" if h else "", _below_100(r) if r else ""]))


def words(amount: float) -> str:
    """Indian numbering: 23970 -> 'INR Twenty Three Thousand Nine Hundred Seventy Only'."""
    rupees, paise = divmod(round(amount * 100), 100)
    parts = []
    for div, name in ((10**7, "Crore"), (10**5, "Lakh"), (1000, "Thousand")):
        q, rupees = divmod(rupees, div)
        if q:
            parts.append(f"{_below_1000(q)} {name}")
    if rupees or not parts:
        parts.append(_below_1000(rupees) if rupees else "Zero")
    return "INR " + " ".join(parts) + (f" and {_below_100(paise)} Paise" if paise else "") + " Only"


def _money(x: float) -> str:
    return f"{x:,.2f}"


def render(s, tenant: Tenant, quote: Quote) -> bytes:
    contact = s.get(Contact, quote.contact_id)
    co = setting(tenant, "company") or {}
    seller = co.get("name") or tenant.name
    hsn = {sku: i.attrs.get("hsn", "") for sku, i in get_items(s, tenant.id, [ln["sku"] for ln in quote.lines]).items()}
    disc = quote.discount_pct or 0
    rows = [(ln["name"], hsn.get(ln["sku"], ""), f"{ln['qty']:g} Nos", ln["unit_price"], disc,
             round(ln["amount"] * (1 - disc / 100), 2)) for ln in quote.lines]
    discount = round(quote.subtotal * disc / 100, 2)
    taxable = round(quote.subtotal - discount, 2)
    total = round(quote.total)
    reg, bold, rupee = _fonts()
    pages = max(1, -(-len(rows) // ROWS_PER_PAGE))

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4, pageCompression=0)
    c.setTitle(f"Receipt {quote.id}")
    c.setLineWidth(0.6)
    c.setStrokeColorRGB(.45, .45, .45)

    def text(x, y, t, font=reg, size=8.5, align="l"):
        c.setFont(font, size)
        {"l": c.drawString, "r": c.drawRightString, "c": c.drawCentredString}[align](x, TOP - y, str(t))

    def line(x1, y1, x2, y2):
        c.line(x1, TOP - y1, x2, TOP - y2)

    def box(x, y, w, h):
        c.rect(x, TOP - y - h, w, h)

    def fit(t, width, font=reg, size=8.5):
        while t and pdfmetrics.stringWidth(t, font, size) > width:
            t = t[:-1]
        return t

    for page in range(pages):
        text((L + R) / 2, 22, "SALES ORDER" + (f"(Page {page + 1})" if page else ""), bold, 12, "c")
        box(L, 30, R - L, 190)                                    # header: seller + buyer on the left, order details on the right
        line(L + 300, 30, L + 300, 220)
        y = 41
        text(L + 5, y, seller, bold, 9)
        for t in (co.get("tagline"), co.get("address"), co.get("owner") and f"PROP.{co['owner']}",
                  co.get("phone") and f"CELL:{co['phone']}", co.get("gstin") and f"GSTIN/UIN: {co['gstin']}",
                  co.get("state") and f"State Name :  {co['state']}, Code : {co.get('state_code', '')}",
                  co.get("email") and f"E-Mail : {co['email']}"):
            if t:
                y += 11
                text(L + 5, y, fit(t, 290))
        line(L, 125, L + 300, 125)
        text(L + 5, 135, "Buyer (Bill to)")
        text(L + 5, 147, fit(f"{contact.name or ''} - {contact.phone or ''}", 290), bold, 8.5)
        y = 147
        for t in simpleSplit(contact.attrs.get("address") or contact.attrs.get("city") or "", reg, 8.5, 290)[:3]:
            y += 11
            text(L + 5, y, t)
        text(L + 5, y + 11, f"State Name       : {contact.attrs.get('state') or co.get('state', '')}")
        mid = L + 300 + 122.5                                       # right side: a grid of labelled cells
        for yy in (60, 90, 120, 150):
            line(L + 300, yy, R, yy)
        line(mid, 30, mid, 150)
        for x, yy, label, val in ((L + 303, 40, "Voucher No.", quote.id), (mid + 3, 40, "Dated", (quote.closed_at or quote.created_at).strftime("%d-%b-%y")),
                                  (mid + 3, 70, "Mode/Terms of Payment", "PAID" if is_paid(quote) else "CREDIT"), (L + 303, 100, "Buyer's Ref./Order No.", quote.id),
                                  (mid + 3, 100, "Other References", None), (L + 303, 130, "Dispatched through", None),
                                  (mid + 3, 130, "Destination", None), (L + 303, 160, "Terms of Delivery", None)):
            text(x, yy, label)
            if val is not None:
                text(x, yy + 12, val, bold)

        # items: one fixed-height block per page so every page looks the same
        box(L, ITEMS_TOP, R - L, ITEMS_END - ITEMS_TOP)
        line(L, ITEMS_TOP + 16, R, ITEMS_TOP + 16)
        x = L
        for i, (w, head) in enumerate(zip(COLS, ["S.No", "MATERIAL DESCRIPTION", "HSN CODE", "Qty", "Price", "Disc %", "Amount"])):
            text(x + w / 2, ITEMS_TOP + 11, head, bold, 8.5, "c")
            x += w
            if i < len(COLS) - 1:
                line(x, ITEMS_TOP, x, ITEMS_END)
        edges = [L + sum(COLS[:i + 1]) for i in range(len(COLS))]
        y = ITEMS_TOP + 16
        for n, (name, code, qty, price, d, amount) in enumerate(rows[page * ROWS_PER_PAGE:(page + 1) * ROWS_PER_PAGE], page * ROWS_PER_PAGE + 1):
            y += ROW
            text(edges[0] - 4, y - 3, n, align="r")
            text(edges[0] + 4, y - 3, fit(name, COLS[1] - 8))
            text(edges[2] - COLS[2] / 2, y - 3, code, align="c")
            text(edges[3] - 4, y - 3, qty, align="r")
            text(edges[4] - 4, y - 3, _money(price), align="r")
            text(edges[5] - 4, y - 3, _money(d) if d else "", align="r")
            text(edges[6] - 4, y - 3, _money(amount), align="r")
        if page < pages - 1:
            text(R - 4, ITEMS_END + 12, f"continued to page number {page + 2}", reg, 8.5, "r")
        else:
            sx = [L, L + 30, L + 245, L + 325, L + 395, L + 470, R]   # edges of: SI No, GST Rate, Sales, Discount, Taxable, Total
            box(L, ITEMS_END, R - L, 63)
            line(L, ITEMS_END + 24, R, ITEMS_END + 24)
            for xx in sx[1:-1]:
                line(xx, ITEMS_END, xx, ITEMS_END + 63)
            for i, head in enumerate((("SI", "No."), ("GST Rate", "%"), ("Sales", "Amount"), ("Discount", "Amount"), ("Taxable", "Amount"), ("Total", "Amount"))):
                for j, h in enumerate(head):
                    text((sx[i] + sx[i + 1]) / 2, ITEMS_END + 10 + 10 * j, h, bold, 8, "c")
            gst_pct = setting(tenant, "pricing.tax_pct") or 0
            rnd = round(total - quote.total, 2)
            for yy, vals, font in ((ITEMS_END + 36, ("1", f"{gst_pct:.2f} %", _money(quote.subtotal), _money(discount), _money(taxable), _money(quote.total)), reg),
                                   (ITEMS_END + 49, ("1", "Round Off", "", "", "", _money(rnd)), reg),
                                   (ITEMS_END + 62, ("", "Totals", _money(quote.subtotal), _money(discount), _money(taxable), f"{rupee} {_money(total)}"), bold)):
                line(L, yy - 10, R, yy - 10)
                for i, v in enumerate(vals):
                    if i == 1:
                        text(sx[1] + 4, yy, v, font)
                    elif i == 0:
                        text(sx[1] - 4, yy, v, font, 8.5, "r")
                    else:
                        text(sx[i + 1] - 4, yy, v, font, 8.5, "r")
            box(L, ITEMS_END + 63, R - L, 44)
            text(L + 5, ITEMS_END + 74, "Amount Chargeable (in words)")
            text(L + 5, ITEMS_END + 90, words(total), bold, 10)
            text(R - 4, ITEMS_END + 102, "E. & O.E", reg, 8.5, "r")
            box(L, ITEMS_END + 107, R - L, 70)
            text(L + 5, ITEMS_END + 140, "Company's PAN")
            text(L + 100, ITEMS_END + 140, f": {co.get('pan', '')}", bold)
            box(L + 272, ITEMS_END + 127, R - L - 272, 50)
            text(R - 4, ITEMS_END + 140, f"for {seller}", bold, 8.5, "r")
            text(R - 4, ITEMS_END + 172, "Authorised Signatory", reg, 8.5, "r")
        text((L + R) / 2 - 40, 820, "This is a Computer Generated Document", reg, 8.5, "c")
        c.showPage()
    c.save()
    return buf.getvalue()


def send(s, tenant: Tenant, quote: Quote) -> bool:
    """Sends the receipt PDF to the customer on their channel. A failed receipt must never undo the payment."""
    try:
        contact = s.get(Contact, quote.contact_id)
        return channels.send_document(
            s, tenant, contact, f"Payment received for order #{quote.id}. Thank you! Your receipt is attached.",
            render(s, tenant, quote), f"receipt-{quote.id}.pdf", quote_id=quote.id)
    except Exception:
        log.exception("receipt for order %s not sent", quote.id)
        return False


def send_due(s, tenant: Tenant, at: datetime) -> int:
    """The scheduler's catch-up: orders that became fully paid in the last 2 days (payment saved in the app, by import or
    straight in the database) and have no receipt yet. A failed send is not retried: it shows as 'failed' in the activity."""
    due = s.scalars(select(Quote).where(Quote.tenant_id == tenant.id, Quote.status == "won", Quote.paid_amount >= Quote.total - PAID_SLACK,
                                        Quote.paid_at >= at - timedelta(days=2))).all()
    if not due:
        return 0
    done = set(s.scalars(select(Activity.data["quote_id"].as_integer()).where(
        Activity.tenant_id == tenant.id, Activity.data["kind"].as_string() == "receipt",
        Activity.data["quote_id"].as_integer().in_([q.id for q in due]))))
    return sum(send(s, tenant, q) for q in due if q.id not in done)
