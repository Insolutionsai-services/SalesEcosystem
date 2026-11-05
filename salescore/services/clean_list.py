"""Tidies a business list before it is imported and called: only real Indian mobile numbers, no duplicates, best prospects first.
Cannot know whether a number is switched on: the call itself finds that out (voicemail and no-answer are logged and never retried)."""
import csv
import io
import re
from bisect import bisect_right

PHONE = ("phone", "mobile", "mobile number", "phone number", "contact", "contact number", "number", "whatsapp", "tel")
NAME = ("name", "contact name", "owner", "owner name", "person")
BUSINESS = ("business", "business name", "business_type", "company", "company name", "shop", "shop name", "firm")
RANK_HINTS = ("sales", "turnover", "revenue", "employee", "staff", "review", "rating")   # headers containing these rank the list unless --rank-by is given
ASCENDING = "01234567890123456789"


def mobile(raw: str) -> tuple[str | None, str]:
    """The first valid Indian mobile in a cell (cells may hold several numbers), as 10 digits; else None and why not."""
    why = "no phone number"
    for part in re.split(r"[,/;\n]| or ", raw or ""):
        d = re.sub(r"\D", "", part)
        if d.startswith("91") and len(d) == 12:
            d = d[2:]
        elif d.startswith("0") and len(d) == 11:
            d = d[1:]
        if not d:
            continue
        if len(d) != 10:
            why = "wrong length"
        elif d[0] not in "6789":
            why = "not a mobile number (landline or toll-free)"
        elif len(set(d)) <= 2 or re.search(r"(\d)\1{6,}", d) or d in ASCENDING or d in ASCENDING[::-1]:
            why = "fake-looking number"
        else:
            return d, ""
    return None, why


def _find(headers: list[str], options: tuple[str, ...]) -> str | None:
    return next((h for h in headers if h.strip().lower() in options), None)


def _num(v: str) -> float | None:
    s = re.sub(r"[^\d.]", "", v or "")
    try:
        return float(s)
    except ValueError:
        return None


def clean(text: str, rank_by: list[str] | None = None) -> tuple[list[dict], list[dict], list[str]]:
    """-> (kept rows best first, rejected rows with the reason, the rank columns used)."""
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    heads = list(rows[0].keys()) if rows else []
    pc, nc, bc = _find(heads, PHONE), _find(heads, NAME), _find(heads, BUSINESS)
    if not pc:
        raise ValueError("No phone column found. Name it phone or mobile.")
    ranks = [h for h in heads if h in rank_by] if rank_by else [h for h in heads if any(k in h.lower() for k in RANK_HINTS)]
    cc = _find(heads, ("consent",))
    kept, rejected = [], []
    for r in rows:
        n, why = mobile(r.get(pc, ""))
        if n:
            kept.append({**r, "_n": n})
        else:
            rejected.append({**r, "rejected_because": why})
    # ranking: each rank column becomes a 0-1 position among the kept rows; a lead's score is their average (a missing value counts as 0)
    for h in ranks:
        vals = sorted(v for v in (_num(r.get(h, "")) for r in kept) if v is not None)
        for r in kept:
            v = _num(r.get(h, ""))
            r.setdefault("_s", []).append(bisect_right(vals, v) / len(vals) if v is not None and vals else 0.0)
    for r in kept:
        r["_score"] = sum(r.get("_s", [0])) / max(len(ranks), 1)
    kept.sort(key=lambda r: -r["_score"])   # stable: without rank columns the file's own order stays
    seen, out = set(), []
    for r in kept:
        if r["_n"] in seen:
            rejected.append({k: v for k, v in r.items() if not k.startswith("_")} | {"rejected_because": "duplicate number"})
            continue
        seen.add(r["_n"])
        rest = {k: v for k, v in r.items() if k not in (pc, nc, bc, cc) and not k.startswith("_")}
        out.append({**rest, "phone": r["_n"], "name": r.get(nc, "") if nc else "", "business": r.get(bc, "") if bc else "",   # last: a stray second "phone" column never replaces the checked number
                    "consent": r.get(cc, "") if cc else "",   # never filled in for you: consent has to be real
                    "lead_rank": len(out) + 1})
    return out, rejected, ranks


def to_csv(rows: list[dict]) -> str:
    cols = list(dict.fromkeys(k for r in rows for k in r))
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()
