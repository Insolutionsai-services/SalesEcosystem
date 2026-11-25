"""AI suggestions for a plot-selling company. Every number is computed here (rules and statistics, no LLM), so it is exact and free:
what leads asked for in their messages against the plots left, how each price band, size and facing sells, alerts with their formulas,
and exact answers to common questions for the "Ask your team" panel."""
import math
import re
import statistics
from collections import defaultdict
from datetime import datetime

from sqlalchemy import func, select

from ..core.models import Activity, Client, Contact, Item, Quote, Tenant
from ..core.playbook import setting
from ..core.utils import now
from . import dues, realestate

LAKH, CRORE = 1e5, 1e7
BANDS = [30, 40, 50, 60, 80, 100]  # price bands in lakh
MATCH_MIN = 70  # a plot is suggested to a lead at or above this match score
FORMULA_GAP = ("Open leads with a stated budget are grouped by wanted facing and budget band (30, 40, 50, 60, 80, 100 lakh; above that, their budget rounded up). "
               "Plots left = available plots priced within the band that have that facing. Short: more leads than plots. Tight: leads are 80% or more of the plots. "
               "The same plot can count for several groups, and a plot within the band can still cost more than one lead's own budget. Size and venture are not used here.")
WEIGHTS = {"budget": .50, "size": .30, "facing": .20}
FORMULA_MATCH = ("Match % = 100 x sum(w x f) / sum(w) over what the lead stated. Budget (w 0.50): f = 1 if price <= budget, falling to 0 at budget x 1.15, "
                  "and a plot more than 15% over budget is dropped. Size (w 0.30): f = 1 - |size - wanted| / (0.25 x wanted). Facing (w 0.20): 1 if it matches, else 0. "
                  "A venture the lead named must match, or the plot is dropped. Size and budget scores never go below 0. Up to 3 plots at 70% or more are suggested. "
                 "What a lead wants is read automatically from their messages and notes, so check the message before acting.")

_MONEY = re.compile(r"(?:rs\.?|inr|₹)?\s*(\d+(?:\.\d+)?)\s*(crores?|cr|lakhs?|lacs?|l)\b", re.I)
_SIZE = re.compile(r"(\d{2,3})\s*(?:sq\.?\s*y(?:ar)?d?s?|yards?|yds?|gajam|gajalu)\b", re.I)
_FACING = re.compile(r"\b(north[\s-]?east|north|east|south|west)[\s-]*facing\b", re.I)


def parse_money(text: str) -> float | None:
    m = _MONEY.search(text or "")
    return float(m.group(1)) * (CRORE if m.group(2).lower().startswith("c") else LAKH) if m else None


def inr(n: float) -> str:
    """Indian digit grouping: 6046200 -> ₹60,46,200"""
    s, tail = f"{round(n):d}", ""
    if len(s) > 3:
        s, tail = s[:-3], "," + s[-3:]
        s = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", s)
    return f"₹{s}{tail}"


def parse_wants(text: str, venture_words: dict[str, str]) -> dict:
    """What one message asks for: budget, size (sq yd), facing, venture."""
    t = text or ""
    low = t.lower()
    wants = {}
    if (b := parse_money(t)) is not None:
        wants["budget"] = b
    if m := _SIZE.search(t):
        wants["size"] = float(m.group(1))
    if m := _FACING.search(t):
        wants["facing"] = re.sub(r"[\s-]+", "-", m.group(1).lower())
    for word, venture in venture_words.items():
        if word in low:
            wants["venture"] = venture
    return wants


def _facing_ok(plot_facing: str | None, wanted: str) -> bool:
    return bool(plot_facing) and set(wanted.split("-")) <= set(plot_facing.lower().split("-"))


def match_score(plot: dict, wants: dict) -> int | None:
    """Percent match, or None if a stated budget rules the plot out."""
    parts = {}
    if "budget" in wants:
        over = plot["price"] / wants["budget"] - 1
        if over > .15:
            return None
        parts["budget"] = 1 if over <= 0 else 1 - over / .15
    if "size" in wants:
        parts["size"] = max(0, 1 - abs(plot["size"] - wants["size"]) / (.25 * wants["size"]))
    if "facing" in wants:
        parts["facing"] = 1.0 if _facing_ok(plot["facing"], wants["facing"]) else 0.0
    if wants.get("venture") and plot["venture"] != wants["venture"]:
        return None
    if not parts:
        return None
    return round(100 * sum(WEIGHTS[k] * v for k, v in parts.items()) / sum(WEIGHTS[k] for k in parts))


def _venture_words(ventures: list[dict]) -> dict[str, str]:
    words = {}
    for v in ventures:
        words[v["name"].lower()] = v["name"]
        words[v["name"].split()[0].lower()] = v["name"]
        if v.get("location"):
            words[v["location"].lower()] = v["name"]
    return words


def _plots(s, tenant) -> list[dict]:
    flat = []
    for v in realestate.plots(s, tenant)["ventures"]:
        flat += [{**p, "venture": v["name"], "location": v.get("location")} for p in v["plots"]]
    return flat


def lead_wants(s, tenant: Tenant, venture_words: dict[str, str]) -> list[dict]:
    """Active leads (not won or lost), with what they asked for: the latest value from their messages, else what we noted on them."""
    leads = s.scalars(select(Contact).where(Contact.tenant_id == tenant.id, Contact.stage.notin_(("won", "lost")))).all()
    msgs = defaultdict(list)
    for a in s.scalars(select(Activity).where(Activity.tenant_id == tenant.id, Activity.type == "msg_in").order_by(Activity.at)):
        msgs[a.contact_id].append(a.body or "")
    out = []
    for c in leads:
        wants, said = {}, {}
        for text in msgs[c.id]:  # later messages override earlier ones
            found = parse_wants(text, venture_words)
            wants |= found
            said |= {k: text for k in found}
        noted = parse_wants(f"{c.attrs.get('budget', '')} {c.attrs.get('looking_for', '')}", venture_words)  # 'budget' attr such as "35L"
        for k, v in noted.items():
            if k not in wants:
                wants[k], said[k] = v, "noted on the lead"
        if wants:
            out.append({"id": c.id, "name": c.name or c.phone, "stage": c.stage, "wants": wants, "from": said,
                        "message": msgs[c.id][-1] if msgs[c.id] else None})
    return out


def _label(wants: dict) -> str:
    bits = [f"under {inr(wants['budget'])}" if "budget" in wants else "", f"{wants['size']:g} sq yd" if "size" in wants else "",
            f"{wants['facing'].title()}-facing" if "facing" in wants else "", wants.get("venture", "")]
    return " · ".join(b for b in bits if b)


def demand(s, tenant: Tenant) -> dict:
    plots = _plots(s, tenant)
    free = [p for p in plots if p["status"] == "available"]
    words = _venture_words([{"name": n, "location": next(p["location"] for p in plots if p["venture"] == n)} for n in {p["venture"] for p in plots}])
    leads = lead_wants(s, tenant, words)
    for lead in leads:
        ranked = sorted(((match_score(p, lead["wants"]), p) for p in free), key=lambda x: -(x[0] or -1))
        lead["label"] = _label(lead["wants"])
        lead["matches"] = [{"sku": p["sku"], "venture": p["venture"], "no": p["no"], "size": p["size"], "facing": p["facing"], "price": p["price"], "score": sc}
                           for sc, p in ranked[:3] if sc is not None and sc >= MATCH_MIN]
    # gaps: leads grouped by (facing, price band) against the free plots that fit that group
    groups = defaultdict(list)
    for lead in leads:
        w = lead["wants"]
        if "budget" in w:
            band = next((b for b in BANDS if w["budget"] <= b * LAKH), math.ceil(w["budget"] / LAKH))
            groups[(w.get("facing"), band)].append(lead["name"])
    gaps = []
    for (facing, band), names in groups.items():
        fit = [p for p in free if p["price"] <= band * LAKH * 1.0 and (not facing or _facing_ok(p["facing"], facing))]
        gaps.append({"facing": facing, "band": band, "leads": len(names), "names": names, "plots_left": len(fit),
                     "short": max(0, len(names) - len(fit)),
                     "text": f"{len(names)} lead{'s' if len(names) != 1 else ''} {'wants' if len(names) == 1 else 'want'} {(facing + '-facing ') if facing else 'a '}plot under ₹{band}L, "
                             f"{'none' if not fit else ('only ' if len(fit) < len(names) else '') + str(len(fit))} {'is' if len(fit) == 1 else 'are'} left",
                     "status": "short" if len(names) > len(fit) else "tight" if len(names) >= .8 * len(fit) else "ok"})
    gaps.sort(key=lambda g: (-g["short"], -g["leads"]))
    return {"leads": sorted(leads, key=lambda x: -(x["matches"][0]["score"] if x["matches"] else 0)), "gaps": gaps, "formula": FORMULA_MATCH, "gap_formula": FORMULA_GAP}


# ---- market: how each group of plots sells --------------------------------------------------------------------------------

def _launch(s, tid: int) -> dict[str, datetime]:
    """When each plot went on sale: the activity written when it was added to the catalog."""
    sku = Activity.data["sku"].as_string()  # one expression for SELECT and GROUP BY: Postgres rejects two separately bound copies
    rows = s.execute(select(sku, func.min(Activity.at)).where(
        Activity.tenant_id == tid, Activity.type == "stock", Activity.body.like("Item added%")).group_by(sku))
    return dict(rows.all())


def market(s, tenant: Tenant, at: datetime) -> dict:
    plots, launched = _plots(s, tenant), _launch(s, tenant.id)
    sold_on = {}
    for q in s.scalars(select(Quote).where(Quote.tenant_id == tenant.id, Quote.status == "won")):
        for ln in q.lines:
            sold_on[ln["sku"]] = q.closed_at or q.created_at
    words = _venture_words([{"name": n, "location": next(p["location"] for p in plots if p["venture"] == n)} for n in {p["venture"] for p in plots}])
    leads = lead_wants(s, tenant, words)
    def band_of(price: float) -> str:
        i = next((i for i, b in enumerate(BANDS) if price <= b * LAKH), len(BANDS))
        if i == 0:
            return f"up to ₹{BANDS[0]}L"
        return f"above ₹{BANDS[-1]}L" if i == len(BANDS) else f"above ₹{BANDS[i - 1]}L to ₹{BANDS[i]}L"

    keys = {"venture": lambda p: p["venture"], "size": lambda p: f"{p['size']} sq yd", "facing": lambda p: p["facing"] or "–",
            "price band": lambda p: band_of(p["price"])}
    waiting = {"venture": lambda l, g: l["wants"].get("venture") == g, "size": lambda l, g: abs(l["wants"].get("size", -999) - float(g.split()[0])) <= .15 * float(g.split()[0]),
               "facing": lambda l, g: "facing" in l["wants"] and _facing_ok(g.lower(), l["wants"]["facing"]),
               "price band": lambda l, g: "budget" in l["wants"] and keys["price band"]({"price": l["wants"]["budget"]}) == g}
    out = {}
    for dim, key in keys.items():
        rows = defaultdict(list)
        for p in plots:
            rows[key(p)].append(p)
        res = []
        for group, ps in rows.items():
            sold = [p for p in ps if p["status"] == "sold"]
            free = [p for p in ps if p["status"] == "available"]
            days_sold = [(sold_on[p["sku"]] - launched[p["sku"]]).days for p in sold
                         if p["sku"] in sold_on and p["sku"] in launched and sold_on[p["sku"]] >= launched[p["sku"]]]
            days_open = [(at - launched[p["sku"]]).days for p in free if p["sku"] in launched]
            rate, wait = len(sold) / len(ps), sum(waiting[dim](l, group) for l in leads)
            avg_open = round(statistics.mean(days_open)) if days_open else None
            ages = [(at - sold_on[p["sku"]]).days for p in sold if p["sku"] in sold_on]
            recent, before = sum(d < WINDOW for d in ages), sum(WINDOW <= d < 2 * WINDOW for d in ages)
            months, buy = advice(len(free), wait, recent, before)
            verdict = ("Demand outruns supply" if rate >= .4 and wait >= len(free) and wait else
                       "Slow: review the price" if len(ps) >= 4 and rate <= .2 and avg_open and avg_open > 45 else "Healthy")
            res.append({"group": group, "plots": len(ps), "sold": len(sold), "booked": len(ps) - len(sold) - len(free), "available": len(free),
                        "order": min(p["price"] if dim == "price band" else p["size"] or 0 if dim == "size" else 0 for p in ps), "sell_through": round(rate, 2),
                        "days_to_sell": round(statistics.mean(days_sold)) if days_sold else None, "days_on_market": avg_open,
                        "leads_waiting": wait, "verdict": verdict, "recent": recent, "before": before, "months_of_stock": months, "advice": buy})
        out[dim] = sorted(res, key=lambda r: (r["order"], str(r["group"])))
    out["formula"] = ("Sell-through = plots sold / all plots in the group (booked plots are not counted as sold). Days to sell = average of sale date - date "
                      "the plot was listed, for sold plots; sales dated before listing are left out. Days on market = average of today - listing date, for plots "
                      "still available. Leads waiting = open leads who asked for this venture, size (within 15%), facing or price band. Demand outruns supply: "
                      "sell-through 40% or more, at least 1 lead waiting and leads waiting not fewer than plots left. Slow: at least 4 plots, sell-through 20% "
                      "or less and plots left listed for more than 45 days on average.")
    return out


# ---- alerts ---------------------------------------------------------------------------------------------------------------

def _fence(xs: list[float], floor: float) -> tuple[float, float, float]:
    """Tukey fence: Q3 + 1.5 x IQR, never below `floor`. Too few points: the floor alone."""
    if len(xs) < 4:
        return 0, 0, floor
    q1, _, q3 = statistics.quantiles(xs, n=4, method="inclusive")
    return q1, q3, max(floor, q3 + 1.5 * (q3 - q1))


def _alert(key: str, title: str, severity: str, formula: str, values: str, items: list[dict]) -> dict:
    return {"key": key, "title": title, "severity": severity if items else "ok", "formula": formula, "values": values, "count": len(items), "items": items}


def alerts(s, tenant: Tenant, at: datetime) -> list[dict]:
    tid, out = tenant.id, []
    names = {c.id: c for c in s.scalars(select(Contact).where(Contact.tenant_id == tid))}
    nm = lambda cid: names[cid].name or names[cid].phone if cid in names else "?"  # noqa: E731
    # 1. leads stuck: idle days against the Tukey fence of all open leads
    last = dict(s.execute(select(Activity.contact_id, func.max(Activity.at)).where(Activity.tenant_id == tid, Activity.contact_id.isnot(None)).group_by(Activity.contact_id)).all())
    idle = {c.id: (at - max(last.get(c.id) or c.created_at, c.created_at)).days for c in names.values() if c.stage not in ("won", "lost")}
    q1, q3, limit = _fence(list(idle.values()), 7)
    stuck = [{"text": f"{nm(cid)} · {names[cid].stage.replace('_', ' ')} · idle {d} days", "contact_id": cid} for cid, d in sorted(idle.items(), key=lambda x: -x[1]) if d > limit]
    out.append(_alert("stuck", "Leads gone idle", "warning", "Idle days = whole days since the last message or update on an open lead. "
                      "Flag if idle is more than the larger of 7 days and Q3 + 1.5 x (Q3 - Q1). With fewer than 4 open leads the limit is 7 days.",
                      (f"Q1 = {q1:g}, Q3 = {q3:g}, " if len(idle) >= 4 else "") + f"limit = {limit:g} days, across {len(idle)} open leads", stuck))
    # 2. a client (broker) gone quiet: days since their last lead against 3 x their usual gap between leads
    quiet = []
    clients = s.scalars(select(Client).where(Client.tenant_id == tid)).all()
    for cl in clients:
        dates = sorted(c.created_at for c in names.values() if c.attrs.get("client") == cl.name)
        gaps = [(b - a).total_seconds() / 86400 for a, b in zip(dates, dates[1:])]
        limit_c = max(14, 3 * statistics.median(gaps)) if len(gaps) >= 2 else 30
        silent = (at - dates[-1]).days if dates else (at - cl.created_at).days
        if silent > limit_c:
            quiet.append({"text": f"{cl.name} · last lead {silent} days ago (usual gap {statistics.median(gaps):.0f} days)" if gaps else f"{cl.name} · no lead for {silent} days", "client": cl.name})
    out.append(_alert("quiet", "Client has gone quiet", "warning", "Silent days = today - date of their latest lead. Flag if silent > max(14, 3 x median gap between their leads); 30 days when they have fewer than 3 leads. A client with no leads is counted from the day they were added.",
                      f"{len(clients)} clients checked", quiet))
    # 3. discounts: below list price, against policy and against the usual
    items = {i.sku: i for i in s.scalars(select(Item).where(Item.tenant_id == tid))}
    rows = []
    for q in s.scalars(select(Quote).where(Quote.tenant_id == tid, Quote.status.in_(("won", "sent", "pending_approval")))):
        known = [ln for ln in q.lines if ln["sku"] in items]  # both sides of the sum use the same lines
        list_total = sum(ln["qty"] * items[ln["sku"]].price for ln in known)
        if list_total:
            agreed = sum(ln["amount"] for ln in known) * (1 - (q.discount_pct or 0) / 100)
            rows.append((q, round(100 * (list_total - agreed) / list_total, 2)))
    ds = [d for _, d in rows]
    mean, sd, cap = (statistics.mean(ds), statistics.pstdev(ds), setting(tenant, "pricing.max_discount_pct")) if ds else (0, 0, 0)
    odd = [{"text": f"{nm(q.contact_id)} · quote #{q.id} · {d:g}% below list price ({inr(q.total)})", "contact_id": q.contact_id}
           for q, d in sorted(rows, key=lambda x: -x[1]) if d > cap or (len(ds) >= 8 and sd and (d - mean) / sd > 2)]
    out.append(_alert("discount", "Discount above normal", "warning", "Discount % = (list price - agreed price) / list price, before tax, using today's catalog price. Flag if above the policy limit, or, once there are 8 or more quotes, if z = (d - mean) / sd > 2.",
                      f"policy limit = {cap:g}%, mean = {mean:.2f}%, sd = {sd:.2f}% over {len(ds)} quotes", odd))
    # 4. a plot quoted to two people
    holders = defaultdict(dict)
    for q in s.scalars(select(Quote).where(Quote.tenant_id == tid, Quote.status.in_(("won", "sent", "pending_approval")))):
        for ln in q.lines:
            if holders[ln["sku"]].get(q.contact_id) != "won":  # a later open quote must not hide that this person bought it
                holders[ln["sku"]][q.contact_id] = q.status
    doubles = []
    for sku, who in holders.items():
        if len(who) > 1:
            sold = [c for c, st in who.items() if st == "won"]
            label = items[sku].name if sku in items else sku
            doubles.append({"text": (f"{label} is sold to {nm(sold[0])} but also quoted to {', '.join(nm(c) for c in who if c != sold[0])}" if sold
                                     else f"{label} is quoted to {' and '.join(nm(c) for c in who)}"), "sold": bool(sold)})
    doubles.sort(key=lambda d: not d["sold"])
    out.append(_alert("double", "Plot quoted to two people", "critical", "For each plot, count the different customers with a live quote (sent, awaiting approval or won). Flag if the count is 2 or more.",
                      f"{len(holders)} plots with a live quote", doubles))
    # 5. dues far older than usual
    pending = dues.pending(s, tenant, at)
    delays = [(q.paid_at - (q.closed_at or q.created_at)).total_seconds() / 86400 for q in s.scalars(select(Quote).where(
        Quote.tenant_id == tid, Quote.status == "won", Quote.paid_at.isnot(None))) if (q.paid_amount or 0) >= q.total - 0.5]
    last_step = max(setting(tenant, "payment_reminder_days") or [14])
    q1, q3, fence = _fence(delays, last_step)
    late = [{"text": f"{d['customer']} · order #{d['quote_id']} · {inr(d['balance'])} pending for {d['age_days']} days", "contact_id": d["contact_id"], "critical": d["age_days"] > 2 * fence,
             "name": d["customer"], "amount": d["balance"], "days": d["age_days"], "order": d["quote_id"]}
            for d in sorted(pending, key=lambda d: -d["age_days"]) if d["age_days"] > fence]
    sev = "critical" if any(x["critical"] for x in late) else "warning"
    out.append(_alert("dues", "Dues far older than usual", sev, "Age = days since the order was won. Flag if age > max(last reminder day, Q3 + 1.5 x IQR of how long paid orders took to pay). Critical beyond 2x that.",
                      f"limit = {fence:g} days (last reminder day {last_step}), from {len(delays)} paid orders", late))
    return out


# ---- exact answers for the "Ask your team" panel --------------------------------------------------------------------------

def quick_answer(s, tenant: Tenant, text: str) -> str | None:
    """Questions about dues, clients, plots and alerts answered straight from the database; None = let the AI handle it."""
    t = text.lower()
    at = now()
    if re.search(r"\b(broker|client)s?\b", t) and re.search(r"pending|dues?|owe|outstanding", t) and re.search(r"most|highest|max|top|biggest|largest", t):
        best = max(realestate.clients(s, tenant)["clients"], key=lambda c: c["due"], default=None)
        return f"{best['name']} has the most pending dues: {inr(best['due'])} across {best['buyers']} buyer{'s' if best['buyers'] != 1 else ''} they brought." if best and best["due"] else "No client has pending dues."
    if re.search(r"\b(best|top)\b.*\b(broker|client)|\b(broker|client)s?\b.*\b(best|top|most sales|highest sales|score)", t):
        rows = realestate.clients(s, tenant)["clients"]
        return "Clients by sales: " + "; ".join(f"{c['name']} {inr(c['value'])} (score {c['score']})" for c in rows[:4]) if rows else None
    if re.search(r"pending|dues|outstanding|who owes|owe", t):
        d = dues.pending(s, tenant, at)
        if not d:
            return "Nothing is pending: every order is fully paid."
        return (f"{inr(sum(x['balance'] for x in d))} is pending on {len(d)} orders. Largest: "
                + "; ".join(f"{x['customer']} {inr(x['balance'])} ({x['age_days']} days)" for x in sorted(d, key=lambda x: -x["balance"])[:3]) + ".")
    m = re.search(r"\b(available|sold|booked|left|remaining)\b", t)
    if m and re.search(r"plots?", t):
        status = {"available": "available", "left": "available", "remaining": "available", "sold": "sold", "booked": "booked"}[m.group(1)]
        vs = realestate.plots(s, tenant)["ventures"]
        chosen = [v for v in vs if v["name"].split()[0].lower() in t or (v.get("location") or "x").lower() in t] or vs
        return f"{status.title()} plots: " + "; ".join(f"{v['name']} {v[status]}" for v in chosen) + f" (total {sum(v[status] for v in chosen)})."
    if re.search(r"alert|stuck|wrong|attention|problem", t):
        hits = [a for a in alerts(s, tenant, at) if a["count"]]
        return ("Needs attention: " + "; ".join(f"{a['title']} ({a['count']})" for a in hits) + ". Open AI suggestions for the details.") if hits else "No alerts right now."
    return None


# ---- what to buy, what to charge, who to collect from and who to call back --------------------------------------------------

WINDOW = 30  # days: "recent" is the last 30 days, "before" the 30 days before that
FORMULA_BUY = ("Recent = plots sold in the last 30 days. Before = plots sold in the 30 days before that. Monthly sales = (recent + before) / 2. "
               "Months of stock = plots left / monthly sales. Buy more: more leads waiting than plots left, or under 3 months of stock. "
               "Hold buying: no sale in 60 days and no lead waiting. Otherwise: enough stock. Booked plots are not counted as left. "
               "A north-east plot also suits a lead who asked for east or north, so one lead can be counted in more than one facing. "
               "This is a rule applied to your past sales, not a promise of future sales.")
FORMULA_PRICE = ("Rate = agreed amount before tax / plot size in sq yd, for plots already sold in the venture. If leads waiting are as many as the plots left "
                 "or more, the minimum is the middle (median) sold rate; otherwise it is the lowest sold rate. When you enter your cost, the minimum is never "
                 "below cost x (1 + margin). Without your cost this says nothing about profit.")
FORMULA_PAY = "Balance = order total - amount paid, for won orders. Age = whole days since the order was won. Overdue = age above the limit used by the dues alert."
FORMULA_ENGAGE = ("Open leads (not won or lost) who asked about a budget, size, facing, venture or plot. Quiet days = whole days since the last message or update. "
                  "The plot shown is the best match at 70% or more. Requirements are read from message text, so read the message before calling.")


def advice(available: int, waiting: int, recent: int, before: int) -> tuple[float | None, str]:
    monthly = (recent + before) / 2
    months = round(available / monthly, 1) if monthly else None
    if waiting > available or (months is not None and months < 3):
        return months, "Buy more"
    if not monthly and not waiting:
        return months, "Hold buying"
    return months, "Enough stock"


def _sales(s, tid: int, plots: list[dict]) -> list[dict]:
    """One row per sold plot: when, and the rate per sq yd actually agreed (before tax)."""
    by_sku = {p["sku"]: p for p in plots}
    out = {}
    for q in s.scalars(select(Quote).where(Quote.tenant_id == tid, Quote.status == "won").order_by(Quote.id)):
        for ln in q.lines:
            p = by_sku.get(ln["sku"])
            if p and p["size"]:
                agreed = ln["amount"] * (1 - (q.discount_pct or 0) / 100)
                out[ln["sku"]] = {"venture": p["venture"], "at": q.closed_at or q.created_at, "rate": agreed / (ln["qty"] * p["size"])}
    return list(out.values())


def pricing(s, tenant: Tenant, at: datetime, waiting: dict[str, int]) -> list[dict]:
    plots = _plots(s, tenant)
    sales = _sales(s, tenant.id, plots)
    out = []
    for venture in dict.fromkeys(p["venture"] for p in plots):
        mine = [x for x in sales if x["venture"] == venture]
        free = [p for p in plots if p["venture"] == venture and p["status"] == "available" and p["size"]]
        rates = [x["rate"] for x in mine]
        recent = [x["rate"] for x in mine if (at - x["at"]).days < WINDOW]
        before = [x["rate"] for x in mine if WINDOW <= (at - x["at"]).days < 2 * WINDOW]
        high = waiting.get(venture, 0) >= len(free) > 0
        listed = round(statistics.median(p["price"] / p["size"] for p in free)) if free else None
        out.append({"venture": venture, "sales": len(rates), "available": len(free), "waiting": waiting.get(venture, 0), "high_demand": high,
                    "low": round(min(rates)) if rates else None, "median": round(statistics.median(rates)) if rates else None,
                    "high": round(max(rates)) if rates else None, "listed": listed,
                    "recent": round(statistics.median(recent)) if recent else None, "before": round(statistics.median(before)) if before else None,
                    "history_min": round(statistics.median(rates) if high else min(rates)) if rates else None})
    return out


def payments(s, tenant: Tenant, at: datetime, overdue: set[int]) -> dict:
    phones = dict(s.execute(select(Contact.id, Contact.phone).where(Contact.tenant_id == tenant.id)).all())
    rows = [{**d, "phone": phones.get(d["contact_id"]), "overdue": d["quote_id"] in overdue}
            for d in sorted(dues.pending(s, tenant, at), key=lambda d: -d["age_days"])]
    total = lambda keep: round(sum(r["balance"] for r in rows if keep(r)), 2)  # noqa: E731
    return {"rows": rows, "total": total(lambda r: True), "overdue": total(lambda r: r["overdue"]),
            "ages": [{"label": "0 to 30 days", "amount": total(lambda r: r["age_days"] <= 30)},
                     {"label": "31 to 60 days", "amount": total(lambda r: 30 < r["age_days"] <= 60)},
                     {"label": "over 60 days", "amount": total(lambda r: r["age_days"] > 60)}]}


def engage(s, tenant: Tenant, at: datetime, leads: list[dict]) -> list[dict]:
    """Everyone still open who asked about plots, longest quiet first, with what they asked and what to offer."""
    wanted = {lead["id"]: lead for lead in leads}
    asked = {}
    for a in s.scalars(select(Activity).where(Activity.tenant_id == tenant.id, Activity.type == "msg_in").order_by(Activity.at)):
        if a.contact_id in wanted and parse_wants(a.body or "", {}) or "plot" in (a.body or "").lower():
            asked[a.contact_id] = a
    last = dict(s.execute(select(Activity.contact_id, func.max(Activity.at)).where(
        Activity.tenant_id == tenant.id, Activity.contact_id.isnot(None)).group_by(Activity.contact_id)).all())
    out = []
    for c in s.scalars(select(Contact).where(Contact.tenant_id == tenant.id, Contact.stage.notin_(("won", "lost")))):
        if c.id not in wanted and c.id not in asked:
            continue
        lead, message = wanted.get(c.id), asked.get(c.id)
        out.append({"id": c.id, "name": c.name or c.phone, "phone": c.phone, "opted_out": c.opted_out,
                    "quiet_days": max(0, (at - max(last.get(c.id) or c.created_at, c.created_at)).days),
                    "asked": message.body if message else None, "asked_at": message.at.isoformat() if message else None,
                    "wants": lead["wants"] if lead else {}, "match": lead["matches"][0] if lead and lead["matches"] else None})
    return sorted(out, key=lambda x: -x["quiet_days"])


def suggestions(s, tenant: Tenant, at: datetime) -> dict:
    """Everything the AI suggestions screen shows, computed once."""
    found, wanted, sold = alerts(s, tenant, at), demand(s, tenant), market(s, tenant, at)
    late = next(a for a in found if a["key"] == "dues")
    waiting = {r["group"]: r["leads_waiting"] for r in sold["venture"]}
    return {"alerts": found, "demand": wanted, "market": sold,
            "pricing": pricing(s, tenant, at, waiting), "payments": payments(s, tenant, at, {i["order"] for i in late["items"]}),
            "engage": engage(s, tenant, at, wanted["leads"]),
            "formulas": {"buy": FORMULA_BUY, "price": FORMULA_PRICE, "pay": FORMULA_PAY, "engage": FORMULA_ENGAGE}}
