"""Round 4: messages, interviews, social sign-in, seeker extras, discounts, invoices, languages, phone app."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.interviews import send_reminders
from app.models import Interview, Message, Notification, OutboxEmail, utcnow
from test_phase1 import client, emails, employer_with_job, env, seeker_ready, signup  # noqa: F401
from test_phase2 import setting  # noqa: F401


def applied(env, seeker_email="juan@test.ph"):
    e, job = employer_with_job(env)
    c, doc = seeker_ready(seeker_email)
    q1 = c.get(f"/api/jobs/{job['id']}").json()["questions"][0]
    r = c.post(f"/api/jobs/{job['id']}/apply", json={"document_ids": [doc], "answers": {str(q1["id"]): "Yes"}})
    assert r.status_code == 201, r.text
    app_id = e.get(f"/api/employer/jobs/{job['id']}/applications").json()["applications"][0]["id"]
    return e, c, job, app_id


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

def test_messaging_thread(env):
    e, c, job, app_id = applied(env)
    # The candidate can't write first
    t = c.get(f"/api/messages/{app_id}").json()
    assert t["can_send"] is False and t["messages"] == []
    assert c.post(f"/api/messages/{app_id}", json={"body": "Hello?"}).status_code == 403

    r = e.post(f"/api/messages/{app_id}", json={"body": "Hi Juan! Are you free for a call this week?"})
    assert r.status_code == 201 and r.json()["mine"] is True
    assert c.get("/api/messages/threads").json()["unread"] == 1
    assert any("New message from Acme PH" in s for s, _ in emails(env, "juan@test.ph"))
    e.post(f"/api/messages/{app_id}", json={"body": "Mornings work best for us."})
    assert sum("New message from Acme PH" in s for s, _ in emails(env, "juan@test.ph")) == 1   # one email per unread batch

    t = c.get(f"/api/messages/{app_id}").json()
    assert t["can_send"] and [m["body"][:2] for m in t["messages"]] == ["Hi", "Mo"]
    assert t["messages"][0]["sender"] == "Boss · Acme PH"
    assert c.get("/api/messages/threads").json()["unread"] == 0             # opening the thread marks it read
    assert c.post(f"/api/messages/{app_id}", json={"body": "  "}).status_code == 422
    assert c.post(f"/api/messages/{app_id}", json={"body": "Yes, Tuesday morning is good."}).status_code == 201
    th = e.get("/api/messages/threads").json()
    assert th["unread"] == 1 and th["threads"][0]["candidate"] == "Juan Dela Cruz"
    assert any("Juan Dela Cruz replied" in s for s, _ in emails(env, "boss@acme.test"))


def test_messages_are_private(env):
    e, c, job, app_id = applied(env)
    other_emp = signup("employer", "other@co.test", "Other")
    other_emp.post("/api/employer/company", json={"name": "Other Co", "industry": "Software & Technology"})
    other_seeker = signup("seeker", "maria@test.ph", "Maria")
    for x in (other_emp, other_seeker):
        assert x.get(f"/api/messages/{app_id}").status_code == 404
        assert x.post(f"/api/messages/{app_id}", json={"body": "hi"}).status_code == 404
    assert client().get(f"/api/messages/{app_id}").status_code == 401


def test_withdrawn_application_closes_thread(env):
    e, c, job, app_id = applied(env)
    e.post(f"/api/messages/{app_id}", json={"body": "Hello"})
    c.post(f"/api/me/applications/{app_id}/withdraw")
    assert e.post(f"/api/messages/{app_id}", json={"body": "Still there?"}).status_code == 400
    assert c.get(f"/api/messages/{app_id}").json()["can_send"] is False


# ---------------------------------------------------------------------------
# Interviews
# ---------------------------------------------------------------------------

def slots(*hours):
    base = (utcnow() + timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
    return [(base + timedelta(hours=h)).isoformat() for h in hours]


def test_interview_flow_with_calendar_invites(env):
    e, c, job, app_id = applied(env)
    s = slots(0, 2, 26)
    r = e.post(f"/api/employer/applications/{app_id}/interviews",
               json={"mode": "video", "location": "https://meet.example.com/abc", "duration_minutes": 45,
                     "notes": "Bring a sample ticket reply.", "slots": s})
    assert r.status_code == 201, r.text
    iv = r.json()
    assert iv["status"] == "proposed" and len(iv["slots"]) == 3 and "Philippine time" in iv["slots"][0]["label"]
    assert e.post(f"/api/employer/applications/{app_id}/interviews", json={"slots": slots(5)}).status_code == 409
    assert c.get("/api/me/applications").json()[0]["status"] == "Interview"
    assert any("Interview invitation" in subj for subj, _ in emails(env, "juan@test.ph"))
    # the invite shows in the message thread, and the candidate can now reply
    t = c.get(f"/api/messages/{app_id}").json()
    assert t["can_send"] and t["messages"][0]["side"] == "system"

    mine = c.get("/api/interviews/mine").json()
    assert mine[0]["id"] == iv["id"] and "ics_url" not in mine[0]
    assert c.post(f"/api/interviews/{iv['id']}/accept", json={"slot": slots(9)[0]}).status_code == 422
    r = c.post(f"/api/interviews/{iv['id']}/accept", json={"slot": s[1]})
    assert r.status_code == 200 and r.json()["status"] == "confirmed"
    assert r.json()["google_url"].startswith("https://calendar.google.com/calendar/render?action=TEMPLATE")

    with env() as db:
        invites = [m for m in db.scalars(select(OutboxEmail).where(OutboxEmail.subject.like("Interview confirmed%")))]
    assert {m.to for m in invites} == {"juan@test.ph", "boss@acme.test"}
    ics = invites[0].attachments[0]
    assert ics["content_type"] == "text/calendar; method=REQUEST"
    body = ics["text"]
    start = datetime.fromisoformat(s[1]).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    assert f"DTSTART:{start}" in body and "METHOD:REQUEST" in body and "\r\n" in body
    assert all(len(line.encode()) <= 75 for line in body.split("\r\n"))
    assert "ATTENDEE;CN=Juan Dela Cruz" in body and "SEQUENCE:1" in body

    dl = e.get(f"/api/interviews/{iv['id']}.ics")
    assert dl.status_code == 200 and dl.headers["content-type"].startswith("text/calendar")
    assert client().get(f"/api/interviews/{iv['id']}.ics").status_code == 401

    # Reminder a day before, sent once
    with env() as db:
        row = db.get(Interview, iv["id"])
        row.starts_at = utcnow() + timedelta(hours=20)
        db.commit()
    assert send_reminders(env) == 1 and send_reminders(env) == 0

    # Employer cancels: both get a CANCEL invite with a higher sequence
    r = e.post(f"/api/interviews/{iv['id']}/cancel", json={"reason": "Role was filled internally"})
    assert r.json()["status"] == "cancelled"
    with env() as db:
        cancel = db.scalars(select(OutboxEmail).where(OutboxEmail.subject.like("Cancelled:%"))).all()
        assert len(cancel) == 2 and "METHOD:CANCEL" in cancel[0].attachments[0]["text"]
        assert "SEQUENCE:2" in cancel[0].attachments[0]["text"]
        assert db.scalar(select(Notification).where(Notification.text.like("%cancelled the interview%")))


def test_interview_other_times_and_reschedule(env):
    e, c, job, app_id = applied(env)
    iv = e.post(f"/api/employer/applications/{app_id}/interviews", json={"mode": "phone", "slots": slots(1)}).json()
    r = c.post(f"/api/interviews/{iv['id']}/decline", json={"reason": "I'm in class until 3pm"})
    assert r.json()["status"] == "declined"
    assert any("needs other interview times" in subj for subj, _ in emails(env, "boss@acme.test"))
    new = slots(30, 31)
    r = e.post(f"/api/employer/interviews/{iv['id']}/reschedule", json={"slots": new, "note": "After 4pm then."})
    assert r.json()["status"] == "proposed" and len(r.json()["slots"]) == 2
    assert c.post(f"/api/interviews/{iv['id']}/accept", json={"slot": new[0]}).json()["status"] == "confirmed"
    lst = e.get("/api/employer/interviews").json()
    assert lst[0]["candidate"] == "Juan Dela Cruz" and lst[0]["when"]


def test_interview_rules(env):
    e, c, job, app_id = applied(env)
    past = (utcnow() - timedelta(hours=1)).isoformat()
    assert e.post(f"/api/employer/applications/{app_id}/interviews", json={"slots": [past]}).status_code == 422
    assert e.post(f"/api/employer/applications/{app_id}/interviews", json={"slots": slots(1, 1)}).status_code == 422
    assert e.post(f"/api/employer/applications/{app_id}/interviews",
                  json={"slots": slots(1), "location": "javascript:alert(1)"}).status_code == 422
    other = signup("seeker", "maria@test.ph", "Maria")
    iv = e.post(f"/api/employer/applications/{app_id}/interviews", json={"slots": slots(1)}).json()
    assert other.post(f"/api/interviews/{iv['id']}/accept", json={"slot": iv["slots"][0]["at"]}).status_code == 404
    assert e.post(f"/api/interviews/{iv['id']}/accept", json={"slot": iv["slots"][0]["at"]}).status_code == 403


# ---------------------------------------------------------------------------
# Social sign-in
# ---------------------------------------------------------------------------

import httpx  # noqa: E402
from urllib.parse import parse_qs, urlsplit  # noqa: E402

from app import oauth  # noqa: E402


@pytest.fixture
def social(setting, monkeypatch):
    for k, v in [("google_client_id", "gid"), ("google_client_secret", "gsecret"), ("facebook_app_id", "fid"),
                 ("facebook_app_secret", "fsecret"), ("linkedin_client_id", "lid"), ("linkedin_client_secret", "lsecret")]:
        setting(k, v)
    people = {}       # code -> userinfo
    seen = {}

    def handler(req: httpx.Request):
        if req.method == "POST" or "access_token" in req.url.path:
            form = parse_qs(req.content.decode()) if req.method == "POST" else parse_qs(req.url.query.decode())
            seen["form"] = form
            code = form["code"][0]
            if code not in people:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "tok-" + code})
        code = req.headers["authorization"].split("tok-")[1]
        return httpx.Response(200, json=people[code])

    monkeypatch.setattr(oauth, "_transport", httpx.MockTransport(handler))
    return people, seen


def social_login(c, provider, code, role="seeker", next_=None, link=False):
    q = f"?role={role}" + (f"&next={next_}" if next_ else "") + ("&link=true" if link else "")
    r = c.get(f"/api/auth/oauth/{provider}/start{q}", follow_redirects=False)
    assert r.status_code == 303, r.text
    loc = urlsplit(r.headers["location"])
    state = parse_qs(loc.query)["state"][0]
    return c.get(f"/api/auth/oauth/{provider}/callback?code={code}&state={state}", follow_redirects=False), parse_qs(loc.query)


def test_providers_listed_only_when_configured(env, setting):
    assert client().get("/api/auth/providers").json() == []
    setting("google_client_id", "x"); setting("google_client_secret", "y")
    assert [p["id"] for p in client().get("/api/auth/providers").json()] == ["google"]
    assert client().get("/api/auth/oauth/facebook/start", follow_redirects=False).status_code == 404


def test_google_sign_up_and_sign_in(env, social):
    people, seen = social
    people["g1"] = {"sub": "111", "email": "Ana@Gmail.com", "email_verified": True, "name": "Ana Reyes"}
    c = client()
    r, q = social_login(c, "google", "g1")
    assert q["code_challenge_method"] == ["S256"] and q["redirect_uri"][0].endswith("/api/auth/oauth/google/callback")
    assert r.status_code == 303 and r.headers["location"] == "/seeker.html"
    assert seen["form"]["code_verifier"]                      # PKCE verifier sent with the code
    me = c.get("/api/auth/me").json()["user"]
    assert me["email"] == "ana@gmail.com" and me["role"] == "seeker" and me["email_verified"]
    assert c.get("/api/me/profile").json()["full_name"] == "Ana Reyes"
    # Next time: same Google account signs in to the same user
    c2 = client()
    r, _ = social_login(c2, "google", "g1", next_="/seeker.html%23applications")
    assert r.headers["location"] == "/seeker.html#applications"
    assert c2.get("/api/auth/me").json()["user"]["id"] == me["id"]


def test_link_existing_account_by_verified_email(env, social):
    people, _ = social
    signup("employer", "boss@acme.test", "Boss")
    people["li"] = {"sub": "li-9", "email": "boss@acme.test", "email_verified": True, "name": "Boss"}
    c = client()
    r, _ = social_login(c, "linkedin", "li")
    assert r.headers["location"] == "/employer.html"
    assert c.get("/api/auth/me").json()["user"]["role"] == "employer"
    ids = c.get("/api/me/identities").json()
    assert ids["has_password"] and ids["linked"][0]["provider"] == "linkedin"


def test_social_sign_in_refusals(env, social):
    people, _ = social
    signup("seeker", "juan@test.ph", "Juan")
    people["unv"] = {"sub": "222", "email": "juan@test.ph", "email_verified": False, "name": "Fake Juan"}
    people["fb"] = {"id": "333", "name": "No Email"}               # Facebook account with only a phone number
    c = client()
    r, _ = social_login(c, "google", "unv")
    assert "oauth_error=unverified_email" in r.headers["location"]
    r, _ = social_login(c, "facebook", "fb")
    assert "oauth_error=no_email" in r.headers["location"]
    assert c.get("/api/auth/me").json()["user"] is None
    # A forged callback (no matching state cookie) is refused
    r = client().get("/api/auth/oauth/google/callback?code=unv&state=made-up", follow_redirects=False)
    assert "oauth_error=expired" in r.headers["location"]
    # State can't be replayed
    c3 = client()
    r1 = c3.get("/api/auth/oauth/google/start", follow_redirects=False)
    st = parse_qs(urlsplit(r1.headers["location"]).query)["state"][0]
    people["ok"] = {"sub": "444", "email": "new@test.ph", "email_verified": True, "name": "New"}
    assert c3.get(f"/api/auth/oauth/google/callback?code=ok&state={st}", follow_redirects=False).headers["location"] == "/seeker.html"
    c3.cookies.set("sinag_oauth", st, path="/api/auth/oauth")
    assert "expired" in c3.get(f"/api/auth/oauth/google/callback?code=ok&state={st}", follow_redirects=False).headers["location"]
    # Open redirects are ignored
    c4 = client()
    r, _ = social_login(c4, "google", "ok", next_="//evil.example")
    assert r.headers["location"] == "/seeker.html"


def test_admins_cannot_use_social_sign_in(env, social):
    people, _ = social
    from app.auth import hash_password
    from app.models import User
    with env() as db:
        db.add(User(email="admin@sinag.ph", password_hash=hash_password("password123"), role="admin", name="Admin",
                    email_verified_at=utcnow()))
        db.commit()
    people["adm"] = {"sub": "555", "email": "admin@sinag.ph", "email_verified": True, "name": "Admin"}
    r, _ = social_login(client(), "google", "adm")
    assert "oauth_error=admin" in r.headers["location"]


def test_unlink_needs_another_way_in(env, social):
    people, _ = social
    people["g"] = {"sub": "777", "email": "solo@test.ph", "email_verified": True, "name": "Solo"}
    c = client()
    social_login(c, "google", "g")
    ids = c.get("/api/me/identities").json()
    assert ids["has_password"] is False
    assert c.delete(f"/api/me/identities/{ids['linked'][0]['id']}").status_code == 400


# ---------------------------------------------------------------------------
# Job seeker extras, discount codes, invoices (test payments only)
# ---------------------------------------------------------------------------

from app.billing import INVOICE_NOTICE, TEST_NOTICE  # noqa: E402

CARD = {"method": "card", "card_number": "4242424242424242", "exp_month": 12, "exp_year": 2030, "cvc": "123"}


@pytest.fixture
def paid(setting):
    setting("billing_enabled", True)
    setting("payment_mode", "sandbox")
    setting("free_live_jobs", 5)


def admin_client(env):
    from app.auth import hash_password
    from app.models import User
    with env() as db:
        db.add(User(email="admin@sinag.ph", password_hash=hash_password("password123"), role="admin", name="Admin",
                    email_verified_at=utcnow()))
        db.commit()
    a = client()
    assert a.post("/api/auth/login", json={"email": "admin@sinag.ph", "password": "password123"}).status_code == 200
    return a


def test_seeker_priority_application(env, paid):
    e, job = employer_with_job(env)
    c, doc = seeker_ready()
    s = c.get("/api/billing/seeker").json()
    assert s["notice"] == TEST_NOTICE and {p["code"] for p in s["products"]} == {"seeker_highlight_30", "seeker_priority_5"}
    assert all(p["audience"] == "seeker" for p in s["products"])
    q1 = c.get(f"/api/jobs/{job['id']}").json()["questions"][0]
    body = {"document_ids": [doc], "answers": {str(q1["id"]): "Yes"}, "priority": True}
    assert c.post(f"/api/jobs/{job['id']}/apply", json=body).status_code == 402
    # employers can't buy seeker products and vice versa
    assert e.post("/api/billing/checkout", json={"product_code": "seeker_priority_5"}).status_code == 404
    assert c.post("/api/billing/checkout", json={"product_code": "job_post"}).status_code == 404
    o = c.post("/api/billing/checkout", json={"product_code": "seeker_priority_5"}).json()["order"]
    assert o["buyer"] == "seeker" and o["number"].startswith("SJ-TEST-")
    assert e.get(f"/api/billing/orders/{o['id']}").status_code == 404           # private to the buyer
    paid_o = c.post(f"/api/billing/orders/{o['id']}/sandbox-pay", json=CARD).json()
    assert paid_o["status"] == "paid"
    assert c.get("/api/billing/seeker").json()["priority_credits"] == 5
    assert c.post(f"/api/jobs/{job['id']}/apply", json=body).status_code == 201
    assert c.get("/api/me/profile").json()["priority_credits"] == 4
    card = e.get(f"/api/employer/jobs/{job['id']}/applications").json()["applications"][0]
    assert card["priority"] is True
    detail = e.get(f"/api/employer/applications/{card['id']}").json()
    assert "priority_credits" not in detail["profile"]


def test_highlighted_profile_sorts_first(env, paid):
    e, job = employer_with_job(env)
    with env() as db:
        from app.models import Company, Subscription
        cid = db.scalar(select(Company.id).where(Company.name == "Acme PH"))
        db.add(Subscription(company_id=cid, product_code="pro", status="active", current_period_end=utcnow() + timedelta(days=20)))
        db.commit()
    a, _ = seeker_ready("a@test.ph")
    b, _ = seeker_ready("b@test.ph")
    for x in (a, b):
        prof = x.get("/api/me/profile").json()
        x.put("/api/me/profile", json={**{k: prof[k] for k in ("full_name", "headline", "skills", "location", "years_experience",
                                                               "desired_salary", "desired_setups", "desired_categories")},
                                       "searchable": True})
    nobody = signup("seeker", "c@test.ph", "Carla Cruz")
    assert nobody.post("/api/billing/checkout", json={"product_code": "seeker_highlight_30"}).status_code == 409   # not searchable
    o = a.post("/api/billing/checkout", json={"product_code": "seeker_highlight_30"}).json()["order"]
    a.post(f"/api/billing/orders/{o['id']}/sandbox-pay", json=CARD)
    b.put("/api/me/profile", json={**b.get("/api/me/profile").json(), "headline": "Updated last"})    # b is more recent
    res = e.get("/api/employer/candidates").json()["candidates"]
    assert res[0]["highlighted"] is True and res[1]["highlighted"] is False
    assert a.get("/api/billing/seeker").json()["highlighted_until"]


def test_discount_codes(env, paid):
    adm = admin_client(env)
    assert adm.post("/api/admin/billing/discounts", json={"code": "both", "percent_off": 10, "amount_off_php": 5}).status_code == 422
    r = adm.post("/api/admin/billing/discounts", json={"code": "launch50", "percent_off": 50, "audience": "employer",
                                                       "product_codes": ["job_post"], "max_uses": 2})
    assert r.status_code == 201 and r.json()["code"] == "LAUNCH50"
    adm.post("/api/admin/billing/discounts", json={"code": "FREEPOST", "percent_off": 100})
    adm.post("/api/admin/billing/discounts", json={"code": "OLD", "amount_off_php": 100,
                                                   "valid_until": (utcnow() - timedelta(days=1)).isoformat()})
    e, job = employer_with_job(env)
    c, _ = seeker_ready()

    # wrong product / audience / expired
    assert e.post("/api/billing/checkout", json={"product_code": "featured_7", "job_id": job["id"], "discount_code": "launch50"}).status_code == 422
    assert c.post("/api/billing/checkout", json={"product_code": "seeker_priority_5", "discount_code": "LAUNCH50"}).status_code == 422
    assert e.post("/api/billing/checkout", json={"product_code": "job_post", "discount_code": "OLD"}).json()["detail"] == "That discount code has expired."

    r = e.post("/api/billing/checkout", json={"product_code": "job_post", "discount_code": "launch50"}).json()["order"]
    assert (r["subtotal_php"], r["discount_php"], r["amount_php"]) == (1499, 750, 749)
    paid_o = e.post(f"/api/billing/orders/{r['id']}/sandbox-pay", json=CARD).json()
    assert paid_o["status"] == "paid" and paid_o["amount_php"] == 749
    # once per account
    again = e.post("/api/billing/checkout", json={"product_code": "job_post"}).json()["order"]
    assert e.post(f"/api/billing/orders/{again['id']}/discount", json={"code": "LAUNCH50"}).json()["detail"] == "You've already used this discount code."
    # 100% off: completes without payment
    free = e.post("/api/billing/checkout", json={"product_code": "job_post", "discount_code": "FREEPOST"}).json()
    assert free["order"]["status"] == "paid" and free["order"]["amount_php"] == 0
    assert e.get("/api/billing/summary").json()["credits"] == 2
    codes = {d["code"]: d for d in adm.get("/api/admin/billing").json()["discounts"]}
    assert codes["LAUNCH50"]["uses"] == 1 and codes["FREEPOST"]["uses"] == 1
    # switched off codes stop working
    adm.patch("/api/admin/billing/discounts/FREEPOST", json={"active": False})
    c_o = c.post("/api/billing/checkout", json={"product_code": "seeker_priority_5"}).json()["order"]
    assert c.post(f"/api/billing/orders/{c_o['id']}/discount", json={"code": "freepost"}).status_code == 422


def test_invoice_is_marked_test(env, paid, setting):
    setting("seller_name", "Sinag Jobs Inc.")
    setting("seller_tin", "123-456-789-000")
    setting("vat_registered", True)
    e, job = employer_with_job(env)
    assert e.put("/api/billing/details", json={"name": "Acme Philippines Inc.", "address": "Cebu IT Park", "tin": "987-654-321-000"}).status_code == 200
    assert e.put("/api/billing/details", json={"tin": "<script>"}).status_code == 422
    o = e.post("/api/billing/checkout", json={"product_code": "job_post"}).json()["order"]
    assert e.get(f"/api/billing/orders/{o['id']}/invoice").status_code == 400          # not paid yet
    e.post(f"/api/billing/orders/{o['id']}/sandbox-pay", json=CARD)
    inv = e.get(f"/api/billing/orders/{o['id']}/invoice").json()
    assert inv["notice"] == INVOICE_NOTICE and inv["test"] is True
    assert inv["bill_to"]["name"] == "Acme Philippines Inc." and inv["bill_to"]["tin"] == "987-654-321-000"
    assert inv["seller"]["tin"] == "123-456-789-000" and inv["total_php"] == 1499
    assert inv["vat"]["vatable_sales"] == 1338.39 and inv["vat"]["vat_12"] == 160.61
    assert inv["payment"]["card"] == "Test Visa ending 4242"
    assert any("invoice.html?order=" in b for _, b in emails(env, "boss@acme.test"))
    # bill-to is frozen at payment time
    e.put("/api/billing/details", json={"name": "Renamed Inc."})
    assert e.get(f"/api/billing/orders/{o['id']}/invoice").json()["bill_to"]["name"] == "Acme Philippines Inc."
    other = signup("seeker", "x@test.ph", "Xavier Lim")
    assert other.get(f"/api/billing/orders/{o['id']}/invoice").status_code == 404


def test_seeker_refund_removes_extra(env, paid):
    adm = admin_client(env)
    c, _ = seeker_ready()
    o = c.post("/api/billing/checkout", json={"product_code": "seeker_priority_5"}).json()["order"]
    c.post(f"/api/billing/orders/{o['id']}/sandbox-pay", json=CARD)
    r = adm.post(f"/api/admin/billing/orders/{o['id']}/refund")
    assert r.status_code == 200 and r.json()["status"] == "refunded"
    assert c.get("/api/billing/seeker").json()["priority_credits"] == 0
    orders = adm.get("/api/admin/billing").json()["orders"]
    assert orders[0]["company"] == "Juan Dela Cruz (job seeker)"


# ---------------------------------------------------------------------------
# Phone app push (Firebase Cloud Messaging)
# ---------------------------------------------------------------------------

def test_phone_app_push_via_fcm(env, setting, tmp_path, monkeypatch):
    import json as _json
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from app import push
    from app.models import DeviceToken
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    sa = tmp_path / "sa.json"
    sa.write_text(_json.dumps({"client_email": "fcm@proj.iam.gserviceaccount.com", "private_key": pem,
                               "token_uri": "https://oauth2.googleapis.com/token"}))
    setting("fcm_project_id", "sinag-test")
    setting("fcm_service_account_file", str(sa))
    push._fcm_token.update(value=None, expires=0)
    sent = []

    def handler(req):
        if req.url.host == "oauth2.googleapis.com":
            assert b"jwt-bearer" in req.content
            return httpx.Response(200, json={"access_token": "ya29.test", "expires_in": 3600})
        body = _json.loads(req.content)
        assert req.headers["authorization"] == "Bearer ya29.test"
        tok = body["message"]["token"]
        sent.append(body)
        if tok.startswith("dead"):
            return httpx.Response(404, json={"error": {"status": "NOT_FOUND", "details": [{"errorCode": "UNREGISTERED"}]}})
        return httpx.Response(200, json={"name": "projects/sinag-test/messages/1"})

    monkeypatch.setattr(push, "_fcm_transport", httpx.MockTransport(handler))
    e, c, job, app_id = applied(env)
    assert c.post("/api/me/devices", json={"token": "x" * 40, "platform": "android"}).status_code == 201
    assert c.post("/api/me/devices", json={"token": "dead" + "y" * 40, "platform": "ios"}).status_code == 201
    assert c.post("/api/me/devices", json={"token": "z" * 40, "platform": "windows"}).status_code == 422
    e.post(f"/api/messages/{app_id}", json={"body": "Can you start Monday?"})
    push.flush_push(env)
    mine = [m for m in sent if "Can you start Monday?" in m["message"]["notification"]["body"]]
    assert len(mine) == 2 and mine[0]["message"]["data"]["url"] == f"/seeker.html#messages-{app_id}"
    with env() as db:
        tokens = [d.token for d in db.scalars(select(DeviceToken))]
    assert tokens == ["x" * 40]                       # the unregistered phone was forgotten
    assert c.post("/api/me/devices/off", json={"token": "x" * 40}).json()["ok"]


# ---------------------------------------------------------------------------
# Languages
# ---------------------------------------------------------------------------

def test_language_files_are_complete_and_safe():
    import json as _json
    import re as _re
    from pathlib import Path
    static = Path(__file__).resolve().parent.parent / "static"
    must = ["This is a test environment. Do not enter real payment details.", "Send application", "Messages",
            "Interviews", "Confirm this time", "Sign in", "Create account"]
    for lang in ("tl", "ceb"):
        data = _json.loads((static / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        strings = data["strings"]
        assert len(strings) > 800
        for k in must:
            assert strings.get(k), (lang, k)
        for k, v in strings.items():
            assert "<" not in v and ">" not in v.replace("->", ""), (lang, k)       # translations never carry HTML
            assert v.strip(), (lang, k)
        for pat, out in data["patterns"]:
            _re.compile(pat)
            groups = _re.compile(pat).groups
            assert all(int(n) <= groups for n in _re.findall(r"\$(\d)", out)), (lang, pat)
    # the brand and the privacy notice are never machine-swapped
    i18n = (static / "i18n.js").read_text()
    assert 'classList.contains("brand")' in i18n
    assert 'translate="no"' in (static / "privacy.html").read_text()


def test_language_choice_is_remembered(env):
    c = signup("seeker", "lang@test.ph", "Lito Ramos")
    assert c.get("/api/auth/me").json()["user"]["lang"] == "en"
    assert c.put("/api/me/lang", json={"lang": "ceb"}).status_code == 200
    assert c.get("/api/auth/me").json()["user"]["lang"] == "ceb"
    assert c.put("/api/me/lang", json={"lang": "fr"}).status_code == 422


def test_phone_app_social_sign_in_handoff(env, social):
    import base64 as _b64
    import hashlib as _h
    people, _ = social
    people["g"] = {"sub": "900", "email": "app@test.ph", "email_verified": True, "name": "App User"}
    verifier = "v" * 64
    challenge = _b64.urlsafe_b64encode(_h.sha256(verifier.encode()).digest()).decode().rstrip("=")
    browser = client()                                     # the phone's browser
    r = browser.get(f"/api/auth/oauth/google/start?app_challenge={challenge}", follow_redirects=False)
    st = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    r = browser.get(f"/api/auth/oauth/google/callback?code=g&state={st}", follow_redirects=False)
    loc = r.headers["location"]
    assert loc.startswith("ph.sinagjobs.app://auth?code=")
    assert browser.get("/api/auth/me").json()["user"] is None          # no session in the browser
    code = parse_qs(urlsplit(loc).query)["code"][0]
    thief = client()
    assert "expired" in thief.get(f"/api/auth/app-login?code={code}&verifier=wrong", follow_redirects=False).headers["location"]
    app = client()                                         # the code was spent by the failed try
    assert "expired" in app.get(f"/api/auth/app-login?code={code}&verifier={verifier}", follow_redirects=False).headers["location"]
    # a fresh code with the right secret works
    r = browser.get(f"/api/auth/oauth/google/start?app_challenge={challenge}", follow_redirects=False)
    st = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    code = parse_qs(urlsplit(browser.get(f"/api/auth/oauth/google/callback?code=g&state={st}", follow_redirects=False).headers["location"]).query)["code"][0]
    r = app.get(f"/api/auth/app-login?code={code}&verifier={verifier}", follow_redirects=False)
    assert r.headers["location"] == "/seeker.html" and app.get("/api/auth/me").json()["user"]["email"] == "app@test.ph"


def test_deleting_account_removes_messages_interviews_and_devices(env):
    from app.models import DeviceToken, Interview, Message
    e, c, job, app_id = applied(env)
    e.post(f"/api/messages/{app_id}", json={"body": "Hello"})
    e.post(f"/api/employer/applications/{app_id}/interviews", json={"slots": slots(3)})
    c.post("/api/me/devices", json={"token": "t" * 40, "platform": "android"})
    c.post(f"/api/messages/{app_id}", json={"body": "Hi!"})
    assert c.post("/api/me/delete-account", json={"password": "password123"}).status_code == 200
    with env() as db:
        assert db.scalar(select(Message.id)) is None
        assert db.scalar(select(Interview.id)) is None
        assert db.scalar(select(DeviceToken.id)) is None


def test_upgrade_allows_seeker_orders_on_old_databases(factory):
    """Databases from Phase 3 have orders.company_id NOT NULL; the upgrader relaxes it and keeps the rows."""
    from sqlalchemy import inspect, text
    from app.db import Base
    from app.migrate import relax_not_null
    engine = factory.kw["bind"]
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO products (code, name, kind, description, price_php, credits, days, limits, active, sort) "
                          "VALUES ('job_post', 'Single job post', 'job_credit', '', 1499, 1, 0, '{}', true, 1)"))
        conn.execute(text("INSERT INTO companies (slug, name, source, source_key, options, verified, active, approval, consecutive_failures, created_at) "
                          "VALUES ('acme', 'Acme', 'direct', '', '{}', true, true, 'approved', 0, CURRENT_TIMESTAMP)"))
        cid = conn.execute(text("SELECT id FROM companies")).scalar()
        conn.execute(text("INSERT INTO orders (number, company_id, product_code, product_name, amount_php, status, provider, is_renewal, test, created_at, buyer, discount_php) "
                          f"VALUES ('SJ-TEST-000001', {cid}, 'job_post', 'Single job post', 1499, 'paid', 'sandbox', false, true, CURRENT_TIMESTAMP, 'employer', 0)"))
        if engine.dialect.name == "postgresql":
            conn.execute(text("ALTER TABLE orders ALTER COLUMN company_id SET NOT NULL"))
    if engine.dialect.name == "sqlite":           # rebuild the table the old way, with NOT NULL
        with engine.connect() as conn:
            ddl = conn.execute(text("SELECT sql FROM sqlite_master WHERE name='orders'")).scalar()
            old = ddl.replace("company_id INTEGER,", "company_id INTEGER NOT NULL,")
            assert old != ddl
            conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
            conn.exec_driver_sql("ALTER TABLE orders RENAME TO orders_tmp")
            for idx in ("ix_orders_company_id", "ix_orders_number", "ix_orders_provider_ref", "ix_orders_status"):
                conn.exec_driver_sql(f"DROP INDEX IF EXISTS {idx}")
            conn.exec_driver_sql(old)
            conn.exec_driver_sql("INSERT INTO orders SELECT * FROM orders_tmp")
            conn.exec_driver_sql("DROP TABLE orders_tmp")
            conn.commit()
    col = lambda: next(c for c in inspect(engine).get_columns("orders") if c["name"] == "company_id")
    assert col()["nullable"] is False
    assert relax_not_null(engine, Base.metadata) == ["orders.company_id"]
    assert col()["nullable"] is True
    with engine.connect() as conn:
        assert conn.execute(text("SELECT number FROM orders")).scalar() == "SJ-TEST-000001"
    assert relax_not_null(engine, Base.metadata) == []
