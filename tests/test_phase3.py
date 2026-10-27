"""Phase 3: test-only billing, analytics, SEO pages and bulk imports."""
import hashlib
import hmac
import json
import re
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app import billing
from app.billing import TEST_NOTICE, run_renewals
from app.models import Job, Order, Subscription, aware, utcnow
from test_phase1 import client, employer_with_job, env, seeker_ready, signup  # noqa: F401
from test_phase2 import setting  # noqa: F401

GOOD = {"method": "card", "card_number": "4242 4242 4242 4242", "exp_month": 12, "exp_year": 2030, "cvc": "123"}
JOB = dict(title="Payroll Specialist", location="Metro Manila", setup="Hybrid", level="Mid level", category="Finance",
           job_type="Full-time", salary_min=30000, salary_max=40000,
           description="Run semi-monthly payroll for 300 staff and file BIR, SSS, PhilHealth and Pag-IBIG reports.")


@pytest.fixture
def paid(setting):
    setting("billing_enabled", True)
    setting("payment_mode", "sandbox")
    setting("free_live_jobs", 1)


def buy(e, code, job_id=None, card=GOOD):
    r = e.post("/api/billing/checkout", json={"product_code": code, "job_id": job_id})
    assert r.status_code == 201, r.text
    assert r.json()["notice"] == TEST_NOTICE
    oid = r.json()["order"]["id"]
    return e.post(f"/api/billing/orders/{oid}/sandbox-pay", json=card).json()


# ---------------------------------------------------------------------------
# Billing
# ---------------------------------------------------------------------------

def test_free_post_then_paywall_then_credit(env, paid):
    e, first = employer_with_job(env)                         # the free post
    assert first["paid_via"] == "free" and first["state"] == "Live"
    assert first["expires_at"] is not None                     # paid posts run up to 30 days
    r = e.post("/api/employer/jobs", json=JOB)
    assert r.status_code == 402 and "free job post" in r.json()["detail"]["message"]
    draft_id = r.json()["detail"]["job_id"]
    assert e.get(f"/api/employer/jobs/{draft_id}").json()["state"] == "Draft"   # nothing lost
    order = buy(e, "job_post")
    assert order["status"] == "paid" and order["number"].startswith("SJ-TEST-") and order["test"] is True
    assert order["card"] == "Visa ending 4242" and "no real charges" in order["notice"]
    assert e.get("/api/billing/summary").json()["credits"] == 1
    r = e.put(f"/api/employer/jobs/{draft_id}", json={**JOB, "action": "publish"})
    assert r.status_code == 200 and r.json()["paid_via"] == "credit"
    s = e.get("/api/billing/summary").json()
    assert s["credits"] == 0 and s["live_jobs"] == 2 and s["notice"] == TEST_NOTICE


def test_real_cards_are_refused_and_never_stored(env, paid):
    e, _ = employer_with_job(env)
    oid = e.post("/api/billing/checkout", json={"product_code": "job_post"}).json()["order"]["id"]
    real_looking = {**GOOD, "card_number": "4532 0151 1283 0366"}          # passes the card checksum, not a test card
    r = e.post(f"/api/billing/orders/{oid}/sandbox-pay", json=real_looking)
    assert r.status_code == 422 and "looks like a real card" in r.json()["detail"] and TEST_NOTICE in r.json()["detail"]
    assert "0366" not in r.text
    assert e.post(f"/api/billing/orders/{oid}/sandbox-pay", json={**GOOD, "card_number": "1234"}).status_code == 422
    assert e.post(f"/api/billing/orders/{oid}/sandbox-pay", json={**GOOD, "exp_year": 2020}).status_code == 422
    assert e.post(f"/api/billing/orders/{oid}/sandbox-pay", json={**GOOD, "cvc": "x"}).status_code == 422
    with env() as s:
        o = s.get(Order, oid)
        assert o.status == "pending" and o.card_last4 is None and o.test is True
        assert not any("4532" in str(getattr(o, c.name)) for c in Order.__table__.columns)


def test_declines_and_ewallets(env, paid):
    e, _ = employer_with_job(env)
    assert buy(e, "job_post", card={**GOOD, "card_number": "4000000000000002"})["failure_reason"] == "The test card was declined."
    assert "insufficient" in buy(e, "job_post", card={**GOOD, "card_number": "4000000000009995"})["failure_reason"]
    assert buy(e, "job_post", card={"method": "gcash", "outcome": "fail"})["status"] == "failed"
    assert buy(e, "job_post", card={"method": "maya", "outcome": "success"})["status"] == "paid"
    assert e.get("/api/billing/summary").json()["credits"] == 1


def test_featured_listing(env, paid):
    e, job = employer_with_job(env)
    employer_with_job(env, email="b@other.test", company="Other Co", title="Newer Job")
    assert client().get("/api/jobs").json()["jobs"][0]["title"] == "Newer Job"
    order = buy(e, "featured_7", job_id=job["id"])
    assert order["status"] == "paid"
    top = client().get("/api/jobs").json()["jobs"][0]
    assert top["id"] == job["id"] and top["featured"] is True
    other_e = signup("employer", "x@x.test")
    assert other_e.post("/api/billing/checkout", json={"product_code": "featured_7", "job_id": job["id"]}).status_code in (409, 422)


def test_subscription_limits_renewal_and_expiry(env, paid):
    e, job = employer_with_job(env)
    assert e.get("/api/employer/candidates").status_code == 402               # resume database needs a plan
    buy(e, "starter")
    s = e.get("/api/billing/summary").json()
    assert s["plan"]["code"] == "starter" and s["resume_db"] and s["live_limit"] == 5 and s["featured_boosts_left"] == 2
    assert e.get("/api/employer/candidates").status_code == 200
    for i in range(4):
        assert e.post("/api/employer/jobs", json={**JOB, "title": f"Role {i}"}).status_code == 201
    assert e.post("/api/employer/jobs", json={**JOB, "title": "Sixth"}).status_code == 402
    assert e.post("/api/billing/use-boost", json={"product_code": "x", "job_id": job["id"]}).status_code == 200
    assert e.get("/api/billing/summary").json()["featured_boosts_left"] == 1

    # Renewal with the saved test card succeeds...
    with env() as db:
        sub = db.scalar(select(Subscription))
        end = aware(sub.current_period_end)
    assert run_renewals(env, now=end + timedelta(minutes=1))["renewed"] == 1
    orders = e.get("/api/billing/summary").json()["orders"]
    assert orders[0]["is_renewal"] and orders[0]["status"] == "paid"
    # ...then switch to the declining test card: the next renewal fails, and after the grace period the plan ends.
    assert e.post("/api/billing/subscription/card", json={"card_number": "4111111111111111"}).status_code == 422
    assert e.post("/api/billing/subscription/card", json={"card_number": "4000 0000 0000 0002"}).status_code == 200
    with env() as db:
        end = aware(db.scalar(select(Subscription).where(Subscription.status == "active")).current_period_end)
    assert run_renewals(env, now=end + timedelta(minutes=1))["failed"] == 1
    assert e.get("/api/billing/summary").json()["subscription"]["status"] == "past_due"
    assert run_renewals(env, now=end + timedelta(days=4))["expired"] == 1
    s = e.get("/api/billing/summary").json()
    assert s["plan"] is None and s["live_jobs"] == 1                          # back to the free allowance


def test_cancel_at_period_end(env, paid):
    e, _ = employer_with_job(env)
    buy(e, "pro")
    assert "stays active" in e.post("/api/billing/subscription/cancel").json()["message"]
    with env() as db:
        end = aware(db.scalar(select(Subscription)).current_period_end)
    assert run_renewals(env, now=end + timedelta(minutes=1))["expired"] == 1


def test_admin_refund_and_prices(env, paid):
    from app import auth
    from app.models import User
    with env() as s:
        s.add(User(email="admin@test.ph", name="Admin", role="admin", password_hash=auth.hash_password("password123"),
                   email_verified_at=utcnow()))
        s.commit()
    a = client()
    a.post("/api/auth/login", json={"email": "admin@test.ph", "password": "password123"})
    e, _ = employer_with_job(env)
    assert a.put("/api/admin/billing/products/job_post", json={"name": "Single job post", "price_php": 999, "credits": 1}).status_code == 200
    order = buy(e, "job_post")
    assert order["amount_php"] == 999
    data = a.get("/api/admin/billing").json()
    assert data["notice"] == TEST_NOTICE and data["orders"][0]["company"] == "Acme PH"
    assert a.post(f"/api/admin/billing/orders/{order['id']}/refund").json()["status"] == "refunded"
    assert e.get("/api/billing/summary").json()["credits"] == 0
    assert e.get("/api/admin/billing").status_code == 403


def test_paymongo_refuses_live_keys(env, setting):
    setting("billing_enabled", True)
    setting("payment_mode", "paymongo_test")
    setting("paymongo_secret_key", "sk_live_abc")
    e, _ = employer_with_job(env)
    r = e.post("/api/billing/checkout", json={"product_code": "job_post"})
    assert r.status_code == 503 and "only accepts PayMongo TEST keys" in r.json()["detail"]
    assert e.get("/api/billing/summary").json()["problem"].startswith("Payments are switched off")


def test_paymongo_test_mode_checkout_and_webhook(env, setting, monkeypatch):
    setting("billing_enabled", True)
    setting("payment_mode", "paymongo_test")
    setting("paymongo_secret_key", "sk_test_123")
    setting("paymongo_webhook_secret", "whsk_test")
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"data": {"id": "cs_test_1", "attributes": {"checkout_url": "https://checkout.paymongo.com/cs_test_1"}}})
    monkeypatch.setattr(billing, "_transport", httpx.MockTransport(handler))
    e, _ = employer_with_job(env)
    r = e.post("/api/billing/checkout", json={"product_code": "job_post"}).json()
    assert r["checkout_url"] == "https://checkout.paymongo.com/cs_test_1"
    req = seen[0]
    body = json.loads(req.content)["data"]["attributes"]
    assert body["line_items"][0]["amount"] == 149900 and body["line_items"][0]["currency"] == "PHP"
    assert body["reference_number"].startswith("SJ-TEST-") and req.headers["authorization"].startswith("Basic ")
    assert e.post(f"/api/billing/orders/{r['order']['id']}/sandbox-pay", json=GOOD).status_code == 400   # simulator off

    def send(event, secret="whsk_test"):
        raw = json.dumps(event).encode()
        t = str(int(utcnow().timestamp()))
        sig = hmac.new(secret.encode(), f"{t}.".encode() + raw, hashlib.sha256).hexdigest()
        return client().post("/api/billing/webhooks/paymongo", content=raw,
                             headers={"Paymongo-Signature": f"t={t},te={sig},li=", "content-type": "application/json"})
    paid_event = {"data": {"attributes": {"type": "checkout_session.payment.paid", "livemode": False, "data": {
        "id": "cs_test_1", "attributes": {"reference_number": r["order"]["number"],
                                          "payments": [{"attributes": {"source": {"type": "card", "brand": "visa", "last4": "4345"}}}]}}}}}
    assert send(paid_event, secret="wrong").status_code == 400
    live = json.loads(json.dumps(paid_event)); live["data"]["attributes"]["livemode"] = True
    assert send(live).json()["ignored"] == "live mode"
    assert e.get("/api/billing/summary").json()["credits"] == 0
    assert send(paid_event).status_code == 200
    assert send(paid_event).status_code == 200                                # repeats are harmless
    assert e.get("/api/billing/summary").json()["credits"] == 1


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

def test_analytics(env):
    from app import auth
    from app.models import User
    e, job = employer_with_job(env)
    ua = {"user-agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/120"}
    c = client()
    c.post("/api/track", json={"type": "visit"}, headers=ua)
    c.post("/api/track", json={"type": "visit"}, headers=ua)
    c.post("/api/track", json={"type": "visit"}, headers={"user-agent": "Googlebot/2.1"})
    c.post("/api/track", json={"type": "search", "q": "  Payroll   Manila "}, headers=ua)
    for _ in range(3):
        c.get(f"/api/jobs/{job['id']}", headers=ua)
    c.get(f"/api/jobs/{job['id']}", headers={"user-agent": "bingbot"})
    s, doc = seeker_ready()
    q1 = s.get(f"/api/jobs/{job['id']}", headers=ua).json()["questions"][0]
    s.post(f"/api/jobs/{job['id']}/apply", json={"document_ids": [doc], "answers": {str(q1["id"]): "Yes"}})
    with env() as db:
        db.add(User(email="admin@test.ph", name="Admin", role="admin", password_hash=auth.hash_password("password123"),
                    email_verified_at=utcnow()))
        db.commit()
    a = client()
    a.post("/api/auth/login", json={"email": "admin@test.ph", "password": "password123"})
    rep = a.get("/api/admin/analytics?days=7").json()
    t = rep["totals"]
    assert (t["visit"], t["unique"], t["job_view"], t["application"]) == (2, 1, 4, 1)
    assert rep["signups"]["seeker"] == 1 and rep["signups"]["employer"] == 1
    assert rep["conversion"]["view_to_apply"] == 25.0
    assert rep["top_jobs"][0]["id"] == job["id"] and rep["top_searches"][0]["q"] == "payroll manila"
    assert rep["recruiters"][0]["company"] == "Acme PH" and rep["recruiters"][0]["applications"] == 1
    mine = e.get("/api/employer/analytics?days=7").json()
    assert mine["totals"] == {"views": 4, "applications": 1} and mine["jobs"][0]["conversion"] == 25.0
    assert e.get("/api/admin/analytics").status_code == 403


# ---------------------------------------------------------------------------
# SEO pages
# ---------------------------------------------------------------------------

def test_job_page_and_structured_data(env):
    e, job = employer_with_job(env, setup="Remote", location="Remote (PH-based)")
    page = client().get(f"/jobs/{job['id']}", follow_redirects=False)
    assert page.status_code == 301 and page.headers["location"] == f"/jobs/{job['id']}/support-associate"
    html = client().get(f"/jobs/{job['id']}/support-associate").text
    ld = [json.loads(x) for x in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)]
    jp = next(x for x in ld if x["@type"] == "JobPosting")
    assert jp["title"] == "Support Associate" and jp["hiringOrganization"]["name"] == "Acme PH"
    assert jp["baseSalary"]["currency"] == "PHP" and jp["baseSalary"]["value"]["unitText"] == "MONTH"
    assert jp["jobLocationType"] == "TELECOMMUTE" and jp["applicantLocationRequirements"]["name"] == "Philippines"
    assert jp["directApply"] is True and jp["employmentType"] == "FULL_TIME"
    assert 'rel="canonical"' in html and "noindex" not in html
    e.post(f"/api/employer/jobs/{job['id']}/close")
    assert client().get(f"/jobs/{job['id']}/support-associate").status_code == 404


def test_crawled_job_pages_defer_to_the_original(env):
    import asyncio, dataclasses
    from app.config import settings
    from app.crawler import crawl
    from app.demo import ensure_demo_companies, fixture_transport
    with env() as s:
        ensure_demo_companies(s)
    asyncio.run(crawl(env, dataclasses.replace(settings, per_host_delay_seconds=0), transport=fixture_transport()))
    j = client().get("/api/jobs").json()["jobs"][0]
    html = client().get(f"/jobs/{j['id']}", follow_redirects=True).text
    assert "noindex" in html and "JobPosting" not in html and f'href="{j["url"]}"' in html


def test_category_pages_sitemap_robots(env):
    employer_with_job(env)
    html = client().get("/jobs/c/customer-support").text
    assert "Customer Support jobs in the Philippines" in html and "Support Associate" in html and "noindex" not in html
    assert "noindex" in client().get("/jobs/c/legal").text                     # empty page: kept out of search
    assert "Customer Support jobs in Cebu" in client().get("/jobs/c/customer-support/l/cebu").text
    assert client().get("/jobs/c/not-a-field").status_code == 404
    assert "By field" in client().get("/jobs").text
    sm = client().get("/sitemap.xml").text
    assert "/jobs/c/customer-support/l/cebu" in sm and "/jobs/1/support-associate" in sm
    rb = client().get("/robots.txt").text
    assert "Disallow: /api/" in rb and "Sitemap:" in rb


# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------

CSV = (b"title,description,reference,location,city,setup,job_type,level,category,salary_min,salary_max,skills,apply_url\n"
       b"Accounting Clerk,Record invoices and reconcile bank accounts for our Pasig office team.,A-1,Metro Manila,Pasig,On-site,Full-time,Entry level,Finance,20000,24000,Excel;QuickBooks,\n"
       b"Remote QA Tester,Test our web app every sprint and write clear bug reports in Jira.,A-2,,,Remote,Contract,,,,,Jira,https://careers.example.com/qa\n"
       b"London Analyst,Analyse data for our London office. Must live in London full time.,A-3,London,London,On-site,,,,,,,\n"
       b"No Description,short,A-4,Cebu,,,,,,,,,\n")


def _admin(env):
    from app import auth
    from app.models import User
    with env() as s:
        s.add(User(email="admin@test.ph", name="Admin", role="admin", password_hash=auth.hash_password("password123"),
                   email_verified_at=utcnow()))
        s.commit()
    a = client()
    a.post("/api/auth/login", json={"email": "admin@test.ph", "password": "password123"})
    return a


def test_csv_backfill(env):
    a = _admin(env)
    files = {"file": ("jobs.csv", CSV, "text/csv")}
    dry = a.post("/api/admin/import", files=files, data={"new_company": "Backfill Corp", "dry_run": "true"}).json()
    assert dry["valid"] == 2 and dry["created"] == 0 and len(dry["errors"]) == 2
    assert {e["row"] for e in dry["errors"]} == {4, 5} and "Philippines" in dry["errors"][0]["error"]
    real = a.post("/api/admin/import", files=files, data={"new_company": "Backfill Corp", "dry_run": "false"}).json()
    assert real["created"] == 2
    cid = real["company"]["id"]
    again = a.post("/api/admin/import", files=files, data={"company_id": str(cid), "dry_run": "false"}).json()
    assert again["created"] == 0 and again["updated"] == 2                     # same references: updated, not duplicated
    jobs = {j["title"]: j for j in client().get("/api/jobs").json()["jobs"]}
    assert jobs["Accounting Clerk"]["easy_apply"] is True and jobs["Accounting Clerk"]["salary_max"] == 24000
    qa = client().get(f"/api/jobs/{jobs['Remote QA Tester']['id']}").json()
    assert qa["easy_apply"] is False and qa["apply_url"] == "https://careers.example.com/qa" and qa["setup"] == "Remote"
    assert client().get("/api/import/template.csv").text.startswith("title,description")


def test_xml_feed_sync_and_xxe_safety(env, monkeypatch):
    from app import importer
    a = _admin(env)
    feed_v1 = b"""<?xml version="1.0"?><source><job><title><![CDATA[Night Shift Nurse]]></title><referencenumber>N-1</referencenumber>
      <description><![CDATA[<p>Care for patients on our night shift at our Iloilo hospital. PRC license required.</p>]]></description>
      <city>Iloilo City</city><country>PH</country><salary>PHP 32,000 - 38,000 per month</salary><jobtype>full-time</jobtype></job>
      <job><title>Lab Technician</title><referencenumber>N-2</referencenumber><description>Run blood tests and keep the lab spotless every day.</description><city>Iloilo City</city><country>PH</country></job></source>"""
    feed_v2 = feed_v1.split(b"<job><title>Lab")[0] + b"</source>"
    payload = {"v": feed_v1}
    monkeypatch.setattr(importer, "fetch_feed", lambda url: payload["v"])
    r = a.post("/api/admin/import/feeds", json={"new_company": "Iloilo Health", "url": "https://feeds.example.com/jobs.xml", "fmt": "xml"}).json()
    assert r["result"]["created"] == 2
    nurse = next(j for j in client().get("/api/jobs").json()["jobs"] if j["title"] == "Night Shift Nurse")
    assert nurse["salary_min"] == 32000 and nurse["location"] == "Iloilo" and nurse["job_type"] == "Full-time"
    payload["v"] = feed_v2
    again = a.post(f"/api/admin/import/feeds/{r['id']}/run").json()
    assert again["closed"] == 1 and again["updated"] == 1
    assert [j["title"] for j in client().get("/api/jobs").json()["jobs"]] == ["Night Shift Nurse"]

    xxe = b"""<?xml version="1.0"?><!DOCTYPE x [<!ENTITY secret SYSTEM "file:///etc/passwd">]>
      <source><job><title>Clerk &secret;</title><description>Filing and phone calls for our Cebu branch office team.</description><city>Cebu</city></job></source>"""
    res = a.post("/api/admin/import", files={"file": ("x.xml", xxe, "application/xml")}, data={"new_company": "X", "dry_run": "true"})
    assert "root:" not in res.text


def test_employer_import_needs_pro(env, paid):
    e, _ = employer_with_job(env)
    r = e.post("/api/employer/import", files={"file": ("jobs.csv", CSV, "text/csv")}, data={"dry_run": "false"})
    assert r.status_code == 402
    buy(e, "pro")
    r = e.post("/api/employer/import", files={"file": ("jobs.csv", CSV, "text/csv")}, data={"dry_run": "false"})
    assert r.json()["created"] == 2


def test_payment_pages_carry_test_notice_and_block_autofill():
    """Every page that takes or shows payment details shows the test notice, and card fields never invite saved cards."""
    from pathlib import Path
    static = Path(__file__).resolve().parent.parent / "static"
    common = (static / "common.js").read_text()
    assert "This is a test environment. Do not enter real payment details." in common
    checkout = (static / "checkout.html").read_text()
    assert "S.testBanner" in checkout and "cc-number" not in checkout and "cc-exp" not in checkout and "cc-csc" not in checkout
    assert 'id="tcn"' in checkout and 'autocomplete="off"' in checkout
    for page in ("employer.html", "admin.html"):
        assert "S.testBanner" in (static / page).read_text()
