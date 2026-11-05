"""Standard customer criteria, live audience counts, and campaign content options (AI / own text, AI poster / upload)."""
import io
import json
from datetime import timedelta

import httpx
import pytest
from PIL import Image
from sqlalchemy import select

from salescore.core.models import Activity, Quote
from salescore.core.utils import now
from salescore.marketing import audience, distribution, orchestrator, poster
from salescore.services import channels, contacts


@pytest.fixture(autouse=True)
def media(tmp_path, monkeypatch):
    monkeypatch.setattr(poster, "MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setattr(channels, "MEDIA_DIR", str(tmp_path / "media"))
    return tmp_path / "media"


@pytest.fixture
def people(s, tenant):
    rows = """name,phone,consent,city,customer_type,source
Asha,9000030001,yes,Chennai,contractor,website_form
Bala,9000030002,yes,Madurai,dealer,indiamart
Chitra,9000030003,no,Chennai,contractor,website_form
Dev,9000030004,yes,Chennai,homeowner,meta_lead_ad
"""
    cs = {c.name: c for c in contacts.import_contacts(s, tenant, rows)}
    old, recent = now() - timedelta(days=200), now() - timedelta(days=5)
    for who, sku, total, when in [("Asha", "PNT-WE-10", 24000, old), ("Bala", "PVC-EL-2", 3500, recent)]:
        s.add(Quote(tenant_id=tenant.id, contact_id=cs[who].id, status="won", subtotal=total, total=total, created_at=when,
                    lines=[{"sku": sku, "name": sku, "qty": 1, "unit_price": total, "amount": total}]))
    s.flush()
    return cs


def names(s, tenant, seg):
    return sorted(c.name for c in audience.segment_contacts(s, tenant, seg))


def test_standard_criteria_combine(s, tenant, people):
    assert names(s, tenant, {"attrs": {"city": ["chennai"], "customer_type": ["contractor", "homeowner"]}}) == ["Asha", "Chitra", "Dev"]
    assert names(s, tenant, {"sources": ["website_form"]}) == ["Asha", "Chitra"]
    assert names(s, tenant, {"bought_skus": ["PNT-WE-10"]}) == ["Asha"]
    assert names(s, tenant, {"bought_within_days": 30}) == ["Bala"]
    assert names(s, tenant, {"not_bought_for_days": 90}) == ["Asha"]
    assert names(s, tenant, {"min_spend": 10000}) == ["Asha"]
    assert names(s, tenant, {"segments": ["never_bought"]}) == ["Chitra", "Dev"]
    assert names(s, tenant, {"attrs": {"city": "Chennai"}, "bought_skus": ["PVC-EL-2"]}) == []  # every criterion must hold


def test_options_and_live_count(s, tenant, people):
    opts = audience.options(s, tenant)
    assert {"value": "Chennai", "count": 3} in opts["details"]["city"]
    assert {o["value"] for o in opts["sources"]} == {"website_form", "indiamart", "meta_lead_ad"}
    assert opts["products"][0]["value"] in {"PNT-WE-10", "PVC-EL-2"}
    p = audience.preview(s, tenant, {"attrs": {"city": ["Chennai"]}, "holdout_pct": 50})
    assert p == {"matched": 3, "reachable": 2, "no_consent": 1, "will_receive": 1, "holdout": 1, "sample": ["Asha", "Dev"]}


def test_own_message_with_ai_poster(s, tenant, people, llm_stub, media):
    llm_stub()  # own words + template poster: no AI call at all
    c = orchestrator.launch_custom(s, tenant, "Diwali offer", "", {"attrs": {"city": ["Chennai"]}},
                                   writer="own", message="Festive colours are in! Visit us this week.", visual="ai_poster")
    assert c.variants[0]["text"].startswith("Hi {name}, Festive") and c.status == "pending_approval"
    assert c.plan["poster"]["headline"] == "Diwali offer"
    with Image.open(media / c.image) as im:
        assert im.size == (1080, 1080)
    orchestrator.redesign_poster(s, tenant, c, headline="Flat 50% off everything")  # above the 10% maximum
    assert c.status == "needs_fix" and any(e.startswith("poster:") for e in c.checks["errors"])


def test_ai_writes_message_and_poster_words(s, tenant, people, llm_stub):
    llm_stub([("text", json.dumps({"variants": [{"label": "A", "angle": "x", "text": "Hi {name}, new stock is in."}],
                                   "poster": {"headline": "New colours", "subline": "Fresh shades for festive homes",
                                              "cta": "Reply to order"}}))])
    c = orchestrator.launch_custom(s, tenant, "Fresh stock", "Announce new shades", {}, visual="ai_poster")
    assert c.plan["poster"] == {"headline": "New colours", "subline": "Fresh shades for festive homes", "cta": "Reply to order"}
    assert c.image and c.checks["ok"]


def test_upload_and_send_with_image(s, tenant, people, media, monkeypatch):
    c = orchestrator.launch_custom(s, tenant, "Shop poster", "", {"attrs": {"city": ["Madurai"]}},
                                   writer="own", message="See our new range.", visual="upload")
    with pytest.raises(ValueError, match="PNG, JPEG or WebP"):
        orchestrator.attach_upload(s, tenant, c, b"not an image")
    buf = io.BytesIO()
    Image.new("RGB", (600, 600), "white").save(buf, "JPEG")
    orchestrator.attach_upload(s, tenant, c, buf.getvalue())
    assert c.image.endswith(".jpg") and (media / c.image).is_file()
    assert orchestrator.approve(s, tenant, c, send_now=True)["sent"] == 1
    sent = s.scalars(select(Activity).where(Activity.type == "msg_out")).one()
    assert sent.data["image"] == c.image  # the poster went out with the message
    with pytest.raises(ValueError, match="already approved"):
        orchestrator.remove_image(s, tenant, c)


def test_social_post_carries_image_url(s, tenant, people, monkeypatch):
    posted = []
    monkeypatch.setattr(distribution.httpx, "post", lambda url, **kw: posted.append(kw["json"]) or httpx.Response(200, request=httpx.Request("POST", url)))
    tenant.playbook = {**tenant.playbook, "distribution": {"webhook_url": "https://hooks.example/social"}}
    c = orchestrator.launch_custom(s, tenant, "Monsoon", "", {}, channel="social", writer="own",
                                   message="Waterproof your roof before the rains.", visual="ai_poster")
    orchestrator.approve(s, tenant, c, send_now=True)
    assert posted[0]["image_url"].endswith(f"/media/{c.image}")


def _wa_and_mail(tenant, monkeypatch):
    """Both channels connected, deliveries captured instead of sent."""
    sent = []
    for k in channels.SMTP_KEYS:
        monkeypatch.setenv(k, "x")
    tenant.wa_phone_id, tenant.wa_token = "1", "t"
    monkeypatch.setattr(channels, "_whatsapp", lambda t, c, text, image=None: sent.append(("whatsapp", c.name)))
    monkeypatch.setattr(channels, "_email", lambda t, c, text, image=None: sent.append(("email", c.name)))
    return sent


def _draft(s, tenant):
    return orchestrator.launch_custom(s, tenant, "Pick", "", {}, writer="own", message="Our new range is in stock.")


def test_campaign_goes_only_where_the_manager_ticked(s, tenant, people, monkeypatch):
    sent = _wa_and_mail(tenant, monkeypatch)
    posted = []
    monkeypatch.setattr(distribution.httpx, "post", lambda url, **kw: posted.append(kw["json"]) or httpx.Response(200, request=httpx.Request("POST", url)))
    tenant.playbook = {**tenant.playbook, "distribution": {"webhook_url": "https://hooks.example/ads"}}
    r = orchestrator.approve(s, tenant, _draft(s, tenant), send_now=True, destinations=["instagram", "google_ads"])
    assert r["status"] == "sent" and r["posted_to"] == ["Instagram", "Google Ads"] and "sent" not in {k for k in r if k == "sent" and r[k]}
    assert sent == [] and posted[0]["destinations"] == ["instagram", "google_ads"]      # no customer got a message

    r = orchestrator.approve(s, tenant, _draft(s, tenant), send_now=True, destinations=["whatsapp"])
    assert r["sent"] > 0 and {ch for ch, _ in sent} == {"whatsapp"} and len(posted) == 1  # no post, no email


def test_email_only_never_uses_whatsapp_and_skips_people_without_email(s, tenant, people, monkeypatch):
    sent = _wa_and_mail(tenant, monkeypatch)
    r = orchestrator.approve(s, tenant, _draft(s, tenant), send_now=True, destinations=["email"])
    assert r["status"] == "sent" and r["sent"] == 0 and sent == []                      # these customers have phone numbers only


def test_a_destination_that_is_not_connected_is_refused_before_anything_goes(s, tenant, people, monkeypatch):
    sent = _wa_and_mail(tenant, monkeypatch)
    c = _draft(s, tenant)
    with pytest.raises(ValueError, match="connect Instagram, Meta Ads first"):
        orchestrator.approve(s, tenant, c, send_now=True, destinations=["whatsapp", "instagram", "meta_ads"])
    with pytest.raises(ValueError, match="pick where"):
        orchestrator.approve(s, tenant, c, send_now=True, destinations=[])
    with pytest.raises(ValueError, match="unknown"):
        orchestrator.approve(s, tenant, c, send_now=True, destinations=["tiktok"])
    assert sent == [] and c.status != "sent"


def test_a_scheduled_campaign_remembers_its_destinations(s, tenant, people, monkeypatch):
    sent = _wa_and_mail(tenant, monkeypatch)
    c = _draft(s, tenant)
    c.send_at = now() + timedelta(hours=2)
    assert orchestrator.approve(s, tenant, c, destinations=["whatsapp"])["status"] == "scheduled" and sent == []
    distribution.dispatch_due(s, tenant, now() + timedelta(hours=3))
    assert c.status == "sent" and c.stats["destination_names"] == ["WhatsApp"] and {ch for ch, _ in sent} == {"whatsapp"}
