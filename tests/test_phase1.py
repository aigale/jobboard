"""Accounts, profiles, documents, Easy Apply, the employer pipeline, admin approval, alerts and upgrades."""
import asyncio
import io
import re
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app import auth, mailer
from app.alerts import daily_digest_due, send_alerts
from app.api import app
from app.config import settings
from app.crawler import crawl
from app.db import get_session, init_db, make_engine
from app.demo import ensure_demo_companies, fixture_transport
from app.models import AlertSubscription, Document, OutboxEmail, User

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"


def docx(text_: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f"<w:document><w:body><w:p><w:r><w:t>{text_}</w:t></w:r></w:p></w:body></w:document>")
    return buf.getvalue()


@pytest.fixture
def env(factory, tmp_path, monkeypatch):
    object.__setattr__(settings, "upload_dir", tmp_path / "uploads")
    mailer.default_factory = factory
    _FACTORY["f"] = factory
    auth._attempts.clear()

    def _session():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_session] = _session
    yield factory
    app.dependency_overrides.clear()
    mailer.default_factory = None


def client():
    return TestClient(app)


_FACTORY = {}


def signup(role, email, name="Test User", verify=True):
    c = client()
    r = c.post("/api/auth/signup", json={"email": email, "password": "password123", "name": name, "role": role, "consent": True})
    assert r.status_code == 201, r.text
    if verify:
        body = emails(_FACTORY["f"], email.lower())[-1][1]
        token = re.search(r"verify\?token=([\w-]+)", body).group(1)
        assert c.get(f"/api/auth/verify?token={token}", follow_redirects=False).status_code == 303
    return c


def employer_with_job(factory, email="boss@acme.test", company="Acme PH", approve=True, **job):
    e = signup("employer", email, "Boss")
    r = e.post("/api/employer/company", json={"name": company, "industry": "Software & Technology", "website": "acme.ph"})
    assert r.status_code == 201, r.text
    if approve:
        with factory() as s:
            from app.models import Company
            c = s.get(Company, r.json()["id"])
            c.approval, c.verified = "approved", True
            s.commit()
    body = dict(title="Support Associate", location="Cebu", setup="Hybrid", level="Entry level", category="Customer Support",
                job_type="Full-time", salary_min=25000, salary_max=30000, skills=["Zendesk", "English"],
                description="Answer merchant chats and emails. " * 4,
                questions=[{"text": "Can you work in Cebu?", "kind": "yesno", "required": True, "knockout_answer": "Yes"},
                           {"text": "Why us?", "kind": "text", "required": False}])
    body.update(job)
    r = e.post("/api/employer/jobs", json=body)
    assert r.status_code == 201, r.text
    return e, r.json()


def seeker_ready(email="juan@test.ph", skills=("Zendesk", "English")):
    c = signup("seeker", email, "Juan Dela Cruz")
    r = c.put("/api/me/profile", json={"full_name": "Juan Dela Cruz", "headline": "Support specialist", "skills": list(skills),
                                       "years_experience": 1, "location": "Cebu", "desired_setups": ["Hybrid"],
                                       "desired_categories": ["Customer Support"], "desired_salary": 25000})
    assert r.status_code == 200, r.text
    doc = c.post("/api/me/documents", files={"file": ("cv.pdf", PDF, "application/pdf")}, data={"kind": "resume"}).json()
    return c, doc["id"]


def emails(factory, to=None):
    with factory() as s:
        q = select(OutboxEmail).order_by(OutboxEmail.id)
        if to:
            q = q.where(OutboxEmail.to == to)
        return [(m.subject, m.body) for m in s.scalars(q)]


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

def test_signup_login_logout(env):
    c = signup("seeker", "Ana@Example.com", "Ana")
    assert c.get("/api/auth/me").json()["user"]["email"] == "ana@example.com"
    dup = client().post("/api/auth/signup", json={"email": "ana@example.com", "password": "password123", "name": "Ana", "role": "seeker", "consent": True})
    assert dup.status_code == 409
    no_consent = client().post("/api/auth/signup", json={"email": "b@example.com", "password": "password123", "name": "B", "role": "seeker"})
    assert no_consent.status_code == 422
    short = client().post("/api/auth/signup", json={"email": "c@example.com", "password": "short", "name": "C", "role": "seeker", "consent": True})
    assert short.status_code == 422 and "8 characters" in short.text
    admin_try = client().post("/api/auth/signup", json={"email": "d@example.com", "password": "password123", "name": "D", "role": "admin", "consent": True})
    assert admin_try.status_code == 422
    c.post("/api/auth/logout")
    assert c.get("/api/auth/me").json()["user"] is None
    assert client().post("/api/auth/login", json={"email": "ana@example.com", "password": "wrong-pass"}).status_code == 401
    c2 = client()
    assert c2.post("/api/auth/login", json={"email": "ANA@example.com", "password": "password123"}).status_code == 200


def test_password_hash_is_not_plain(env):
    signup("seeker", "hash@test.ph")
    with env() as s:
        h = s.scalar(select(User.password_hash).where(User.email == "hash@test.ph"))
    assert h.startswith("scrypt$") and "password123" not in h
    assert auth.verify_password("password123", h) and not auth.verify_password("password124", h)


def test_password_reset(env):
    signup("seeker", "reset@test.ph")
    r = client().post("/api/auth/forgot", json={"email": "reset@test.ph"})
    assert r.status_code == 200
    unknown = client().post("/api/auth/forgot", json={"email": "nobody@test.ph"})
    assert unknown.json()["message"] == r.json()["message"]     # same answer: no account probing
    body = emails(env, "reset@test.ph")[-1][1]
    token = re.search(r"token=([\w-]+)", body).group(1)
    c = client()
    assert c.post("/api/auth/reset", json={"token": token, "password": "brandnew123"}).status_code == 200
    assert c.post("/api/auth/reset", json={"token": token, "password": "another123"}).status_code == 400
    assert client().post("/api/auth/login", json={"email": "reset@test.ph", "password": "brandnew123"}).status_code == 200


def test_login_rate_limit(env):
    signup("seeker", "rl@test.ph")
    c = client()
    codes = [c.post("/api/auth/login", json={"email": "rl@test.ph", "password": "nope-nope"}).status_code for _ in range(11)]
    assert codes[:10] == [401] * 10 and codes[10] == 429


def test_cross_site_writes_blocked(env):
    r = client().post("/api/auth/login", json={"email": "x@test.ph", "password": "password123"},
                      headers={"origin": "https://evil.example"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

def test_document_rules(env):
    c = signup("seeker", "docs@test.ph")
    fake = c.post("/api/me/documents", files={"file": ("cv.pdf", b"not a pdf", "application/pdf")}, data={"kind": "resume"})
    assert fake.status_code == 422 and "real .pdf" in fake.json()["detail"]
    exe = c.post("/api/me/documents", files={"file": ("cv.exe", b"MZ..", "application/octet-stream")}, data={"kind": "resume"})
    assert exe.status_code == 422
    big = c.post("/api/me/documents", files={"file": ("cv.pdf", PDF + b"0" * (6 * 1024 * 1024), "application/pdf")}, data={"kind": "resume"})
    assert big.status_code == 413
    ok = c.post("/api/me/documents", files={"file": ("My CV.docx", docx("Python FastAPI PostgreSQL"), "application/octet-stream")},
                data={"kind": "resume"})
    assert ok.status_code == 201 and ok.json()["has_text"]
    with env() as s:
        assert "FastAPI" in s.get(Document, ok.json()["id"]).text
    assert c.get(f"/api/me/documents/{ok.json()['id']}/file").status_code == 200
    other = signup("seeker", "other@test.ph")
    assert other.get(f"/api/me/documents/{ok.json()['id']}/file").status_code == 404


# ---------------------------------------------------------------------------
# Easy Apply and the pipeline
# ---------------------------------------------------------------------------

def test_full_hiring_flow(env):
    e, job = employer_with_job(env)
    c, doc = seeker_ready()
    detail = c.get(f"/api/jobs/{job['id']}").json()
    assert detail["easy_apply"] and detail["apply_url"] is None
    assert "knockout_answer" not in detail["questions"][0]          # screening answers stay secret
    assert detail["match"]["score"] >= 80
    q1, q2 = detail["questions"]

    missing = c.post(f"/api/jobs/{job['id']}/apply", json={"document_ids": [doc], "answers": {}})
    assert missing.status_code == 422 and "Can you work in Cebu?" in missing.json()["detail"]
    no_resume = c.post(f"/api/jobs/{job['id']}/apply", json={"document_ids": [], "answers": {str(q1["id"]): "Yes"}})
    assert no_resume.status_code == 422 and "resume" in no_resume.json()["detail"]
    r = c.post(f"/api/jobs/{job['id']}/apply", json={"document_ids": [doc], "answers": {str(q1["id"]): "Yes"}, "cover_letter": "Hi"})
    assert r.status_code == 201
    assert c.post(f"/api/jobs/{job['id']}/apply", json={"document_ids": [doc], "answers": {str(q1["id"]): "Yes"}}).status_code == 409
    assert any("New applicant" in s for s, _ in emails(env, "boss@acme.test"))

    apps = c.get("/api/me/applications").json()
    assert apps[0]["status"] == "Submitted"
    board = e.get(f"/api/employer/jobs/{job['id']}/applications").json()
    card = board["applications"][0]
    assert card["score"] >= 80 and not card["viewed"]
    detail = e.get(f"/api/employer/applications/{card['id']}").json()
    assert detail["profile"]["email"] == "juan@test.ph" and detail["documents"][0]["id"] == doc
    assert e.get(f"/api/employer/applications/{card['id']}/documents/{doc}").status_code == 200
    assert c.get("/api/me/applications").json()[0]["status"] == "Viewed"

    e.patch(f"/api/employer/applications/{card['id']}", json={"stage": "screening"})
    assert c.get("/api/me/applications").json()[0]["status"] == "Under review"
    assert not any("Under review" in b for _, b in emails(env, "juan@test.ph"))    # quiet stage: no email
    e.patch(f"/api/employer/applications/{card['id']}", json={"stage": "interview", "message": "Can you do Tuesday 10 AM?"})
    mine = c.get("/api/me/applications").json()[0]
    assert mine["status"] == "Interview" and mine["timeline"][-1]["status"] == "Invited to interview"
    assert any("Tuesday 10 AM" in b for _, b in emails(env, "juan@test.ph"))
    e.post(f"/api/employer/applications/{card['id']}/notes", json={"text": "Great energy on the call"})
    assert "Great energy" not in str(c.get("/api/me/applications").json())          # notes are private
    assert c.get("/api/me/notifications").json()["unread"] >= 1


def test_knockout_and_withdraw(env):
    e, job = employer_with_job(env)
    c, doc = seeker_ready()
    q1 = c.get(f"/api/jobs/{job['id']}").json()["questions"][0]
    c.post(f"/api/jobs/{job['id']}/apply", json={"document_ids": [doc], "answers": {str(q1["id"]): "No"}})
    card = e.get(f"/api/employer/jobs/{job['id']}/applications").json()["applications"][0]
    assert card["score"] <= 20 and card["flags"][0].startswith("Didn't meet")
    app_id = c.get("/api/me/applications").json()[0]["id"]
    assert c.post(f"/api/me/applications/{app_id}/withdraw").status_code == 200
    assert e.get(f"/api/employer/jobs/{job['id']}/applications").json()["applications"] == []
    again = c.post(f"/api/jobs/{job['id']}/apply", json={"document_ids": [doc], "answers": {str(q1["id"]): "Yes"}})
    assert again.status_code == 201
    assert c.get("/api/me/applications").json()[0]["status"] == "Submitted"


def test_permissions_between_accounts(env):
    e1, job1 = employer_with_job(env)
    e2, job2 = employer_with_job(env, email="other@rival.test", company="Rival Inc")
    c, doc = seeker_ready()
    q1 = c.get(f"/api/jobs/{job1['id']}").json()["questions"][0]
    c.post(f"/api/jobs/{job1['id']}/apply", json={"document_ids": [doc], "answers": {str(q1["id"]): "Yes"}})
    app_id = e1.get(f"/api/employer/jobs/{job1['id']}/applications").json()["applications"][0]["id"]
    assert e2.get(f"/api/employer/jobs/{job1['id']}/applications").status_code == 404
    assert e2.get(f"/api/employer/applications/{app_id}").status_code == 404
    assert e2.get(f"/api/employer/applications/{app_id}/documents/{doc}").status_code == 404
    assert e2.put(f"/api/employer/jobs/{job1['id']}", json={}).status_code in (404, 422)
    assert c.get(f"/api/employer/jobs/{job1['id']}/applications").status_code == 403
    assert e1.get("/api/me/profile").status_code == 403
    assert client().get("/api/me/applications").status_code == 401
    assert c.get("/api/admin/overview").status_code == 403


def test_cannot_easy_apply_to_crawled_job(env):
    with env() as s:
        ensure_demo_companies(s)
    import dataclasses
    asyncio.run(crawl(env, dataclasses.replace(settings, per_host_delay_seconds=0), transport=fixture_transport()))
    c, doc = seeker_ready()
    crawled = client().get("/api/jobs").json()["jobs"][0]
    assert not crawled["easy_apply"]
    r = c.post(f"/api/jobs/{crawled['id']}/apply", json={"document_ids": [doc], "answers": {}})
    assert r.status_code == 400 and "company's own site" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Visibility: approval, scheduling, drafts, closing
# ---------------------------------------------------------------------------

def test_pending_company_jobs_stay_hidden_until_approved(env):
    e, job = employer_with_job(env, approve=False)
    assert job["state"] == "Waiting for approval"
    assert client().get(f"/api/jobs/{job['id']}").status_code == 404
    with env() as s:
        s.add(User(email="admin@test.ph", name="Admin", role="admin", password_hash=auth.hash_password("password123")))
        s.commit()
    a = client()
    a.post("/api/auth/login", json={"email": "admin@test.ph", "password": "password123"})
    pending = a.get("/api/admin/companies?approval=pending").json()
    assert pending[0]["name"] == "Acme PH" and pending[0]["owner"]["email"] == "boss@acme.test"
    assert a.post(f"/api/admin/companies/{pending[0]['id']}/decision", json={"decision": "rejected"}).status_code == 422
    assert a.post(f"/api/admin/companies/{pending[0]['id']}/decision", json={"decision": "approved"}).status_code == 200
    assert client().get(f"/api/jobs/{job['id']}").status_code == 200
    assert e.get("/api/employer/jobs").json()[0]["state"] == "Live"


def test_schedule_draft_close(env):
    future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    e, job = employer_with_job(env, publish_at=future)
    assert job["state"] == "Scheduled"
    assert client().get(f"/api/jobs/{job['id']}").status_code == 404
    body = {k: job[k] for k in ("title", "location", "setup", "shift", "level", "category", "job_type", "salary_min", "salary_max", "skills", "perks")}
    body.update(description=job["description"], questions=[], action="publish", publish_at=None)
    assert e.put(f"/api/employer/jobs/{job['id']}", json=body).json()["state"] == "Live"
    assert client().get(f"/api/jobs/{job['id']}").status_code == 200
    assert e.delete(f"/api/employer/jobs/{job['id']}").status_code == 400      # live jobs are closed, not deleted
    assert e.post(f"/api/employer/jobs/{job['id']}/close").json()["state"] == "Closed"
    assert client().get(f"/api/jobs/{job['id']}").status_code == 404
    _, draft = employer_with_job(env, email="d@acme2.test", company="Draft Co", action="draft")
    assert draft["state"] == "Draft"
    expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    bad = e.post("/api/employer/jobs", json={**body, "expires_at": expired, "publish_at": None})
    assert bad.status_code == 201 and bad.json()["state"] == "Expired"


def test_job_form_validation(env):
    e, job = employer_with_job(env)
    base = dict(title="QA Tester", location="Cebu", setup="Remote", level="Mid level", category="Engineering",
                description="x" * 60)
    assert e.post("/api/employer/jobs", json={**base, "location": "Mars"}).status_code == 422
    assert e.post("/api/employer/jobs", json={**base, "salary_min": 50000, "salary_max": 30000}).status_code == 422
    assert e.post("/api/employer/jobs", json={**base, "questions": [{"text": "Pick one", "kind": "choice", "options": ["A"]}]}).status_code == 422
    assert e.post("/api/employer/jobs", json={**base, "questions": [{"text": "Ready?", "kind": "yesno", "knockout_answer": "Maybe"}]}).status_code == 422
    assert e.post("/api/employer/jobs", json={**base, "description": "short"}).status_code == 422


def test_html_in_job_description_is_escaped(env):
    e, job = employer_with_job(env, description="<script>alert(1)</script> We hire great people for our Cebu team.\n\n- one\n- two")
    html = client().get(f"/api/jobs/{job['id']}").json()["description_html"]
    assert "<script>" not in html and "&lt;script&gt;" in html and "<li>one</li>" in html


# ---------------------------------------------------------------------------
# Search filters, matches, alerts
# ---------------------------------------------------------------------------

def test_new_filters(env):
    employer_with_job(env)
    employer_with_job(env, email="b@beta.test", company="Beta Health", title="Clinic Nurse", category="Healthcare",
                      job_type="Part-time", salary_min=40000, salary_max=45000, skills=["Nursing"])
    c = client()
    assert c.get("/api/jobs", params={"job_type": "Part-time"}).json()["total"] == 1
    assert c.get("/api/jobs", params={"industry": "Software & Technology"}).json()["total"] == 2
    assert c.get("/api/jobs", params={"max_pay": 35000}).json()["total"] == 1
    assert c.get("/api/jobs", params={"min_pay": 35000, "max_pay": 50000}).json()["total"] == 1
    assert c.get("/api/jobs", params={"easy_apply": True}).json()["total"] == 2
    assert c.get("/api/jobs", params={"q": "zendesk"}).json()["total"] == 1          # skills are searchable
    f = c.get("/api/facets").json()
    assert {x["value"] for x in f["job_type"]} == {"Full-time", "Part-time"} and f["easy_apply"] == 2


def test_matches_and_alerts(env):
    employer_with_job(env)
    c, _ = seeker_ready()
    m = c.get("/api/me/matches").json()
    assert m["ready"] and m["jobs"][0]["match"]["score"] >= 80
    r = c.post("/api/me/alerts", json={"category": "Customer Support", "frequency": "daily", "use_profile": True})
    assert r.status_code == 201
    with env() as s:       # pretend the alert was last sent yesterday, before the job was posted
        a = s.get(AlertSubscription, r.json()["id"])
        a.last_sent_at = datetime.now(timezone.utc) - timedelta(days=1)
        s.commit()
    assert send_alerts(env, "daily") == 1
    subject, body = emails(env, "juan@test.ph")[-1]
    assert "1 new job" in subject and "Support Associate" in body and "% match" in body
    assert send_alerts(env, "daily") == 0                                             # nothing new since
    token = re.search(r"token=([\w-]+)", body).group(1)
    assert "Unsubscribed" in client().get(f"/api/alerts/unsubscribe?token={token}").text
    assert c.get("/api/me/alerts").json() == []


def test_daily_digest_runs_once_a_day(env):
    manila_7am = datetime(2026, 10, 26, 23, 0, tzinfo=timezone.utc)   # 7 AM Manila on the 27th
    assert not daily_digest_due(env, now=manila_7am)
    assert daily_digest_due(env, now=manila_7am + timedelta(hours=1))
    assert not daily_digest_due(env, now=manila_7am + timedelta(hours=5))
    assert daily_digest_due(env, now=manila_7am + timedelta(days=1, hours=1))


def test_delete_account_removes_files(env):
    c, doc = seeker_ready()
    with env() as s:
        stored = s.get(Document, doc).stored_name
    path = settings.upload_dir / stored
    assert path.exists()
    assert c.post("/api/me/delete-account", json={"password": "wrong"}).status_code == 400
    assert c.post("/api/me/delete-account", json={"password": "password123"}).status_code == 200
    assert not path.exists()
    with env() as s:
        assert s.scalar(select(User).where(User.email == "juan@test.ph")) is None
        assert s.get(Document, doc) is None


# ---------------------------------------------------------------------------
# Crawler and upgrades
# ---------------------------------------------------------------------------

def test_crawler_leaves_employer_jobs_alone(env):
    import dataclasses
    e, job = employer_with_job(env)
    with env() as s:
        ensure_demo_companies(s)
    cfg = dataclasses.replace(settings, per_host_delay_seconds=0, close_after_missed_crawls=1)
    for _ in range(2):
        r = asyncio.run(crawl(env, cfg, transport=fixture_transport()))
        assert r.companies_failed == 0
    assert client().get(f"/api/jobs/{job['id']}").status_code == 200


def test_upgrade_old_database(tmp_path):
    from conftest import fresh_engine
    engine = fresh_engine(tmp_path)
    with engine.begin() as conn:     # the tables as they were before accounts existed
        conn.execute(text("CREATE TABLE companies (id INTEGER PRIMARY KEY, slug VARCHAR(120), name VARCHAR(200), source VARCHAR(40), source_key VARCHAR(500), careers_url VARCHAR(500), logo_url VARCHAR(500), options JSON, verified BOOLEAN, active BOOLEAN, last_crawled_at TIMESTAMP, last_status VARCHAR(20), last_error TEXT, consecutive_failures INTEGER, created_at TIMESTAMP)"))
        conn.execute(text("CREATE TABLE jobs (id INTEGER PRIMARY KEY, company_id INTEGER, external_id VARCHAR(200), title VARCHAR(300), url VARCHAR(1000), apply_url VARCHAR(1000), department VARCHAR(200), employment_type VARCHAR(60), location_raw VARCHAR(500), location VARCHAR(80), city VARCHAR(120), ph_eligibility VARCHAR(30), setup VARCHAR(20), shift VARCHAR(20), level VARCHAR(20), category VARCHAR(60), perks VARCHAR(500), salary_min INTEGER, salary_max INTEGER, salary_original VARCHAR(200), description_html TEXT, description_text TEXT, posted_at TIMESTAMP, first_seen_at TIMESTAMP, last_seen_at TIMESTAMP, missed_crawls INTEGER, status VARCHAR(10), closed_at TIMESTAMP, content_hash VARCHAR(64), featured BOOLEAN)"))
        conn.execute(text("CREATE TABLE alert_subscriptions (id INTEGER PRIMARY KEY, email VARCHAR(320), category VARCHAR(60), location VARCHAR(80), created_at TIMESTAMP, last_sent_at TIMESTAMP, active BOOLEAN)"))
        conn.execute(text("INSERT INTO companies (id, slug, name, source, source_key, options, verified, active, consecutive_failures) VALUES (1, 'old', 'Old Co', 'greenhouse', 'old', '{}', TRUE, TRUE, 0)"))
        conn.execute(text("INSERT INTO jobs (id, company_id, external_id, title, url, location, ph_eligibility, setup, shift, level, category, perks, posted_at, status, content_hash, featured, missed_crawls) VALUES (1, 1, 'x', 'Old job', 'https://x', 'Cebu', 'ph_location', 'On-site', 'Day shift', 'Mid level', 'Other', '', '2026-01-01', 'open', 'h', FALSE, 0)"))
    init_db(engine)
    init_db(engine)                  # running it twice is harmless
    with engine.connect() as conn:
        row = conn.execute(text("SELECT origin, job_type, easy_apply, view_count FROM jobs")).one()
        assert (row[0], row[1], bool(row[2]), row[3]) == ("crawled", "Not stated", False, 0)
        assert conn.execute(text("SELECT approval FROM companies")).scalar() == "approved"
        assert conn.execute(text("SELECT count(*) FROM users")).scalar() == 0


def test_deleting_employer_takes_jobs_down(env):
    e, job = employer_with_job(env)
    assert client().get(f"/api/jobs/{job['id']}").status_code == 200
    assert e.post("/api/me/delete-account", json={"password": "password123"}).status_code == 200
    assert client().get(f"/api/jobs/{job['id']}").status_code == 404
