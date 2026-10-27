"""Phase 1 fixes (email confirmation, push) and Phase 2 (team, company pages, resume database, tests, chat, AI)."""
import base64
import json
import os
import re
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app import ai, push
from app.config import settings
from app.models import (Assessment, AssessmentAttempt, CandidateInvite, Notification, PushMessage, PushSubscription,
                        utcnow)
from test_phase1 import PDF, client, docx, emails, employer_with_job, env, seeker_ready, signup  # noqa: F401

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


@pytest.fixture
def setting():
    old = {}

    def _set(name, value):
        old.setdefault(name, getattr(settings, name))
        object.__setattr__(settings, name, value)
    yield _set
    for k, v in old.items():
        object.__setattr__(settings, k, v)


def last_link(factory, to, pattern):
    body = emails(factory, to)[-1][1]
    return re.search(pattern, body).group(1)


# ---------------------------------------------------------------------------
# Email confirmation
# ---------------------------------------------------------------------------

def test_unconfirmed_accounts_cannot_apply_or_publish(env):
    e, job = employer_with_job(env)
    c = signup("seeker", "new@test.ph", verify=False)
    assert c.get("/api/auth/me").json()["user"]["email_verified"] is False
    c.put("/api/me/profile", json={"full_name": "New", "headline": "Support", "skills": ["Zendesk"]})
    doc = c.post("/api/me/documents", files={"file": ("cv.pdf", PDF, "application/pdf")}, data={"kind": "resume"}).json()["id"]
    r = c.post(f"/api/jobs/{job['id']}/apply", json={"document_ids": [doc], "answers": {}})
    assert r.status_code == 403 and "Confirm your email" in r.json()["detail"]
    assert "new link" in c.post("/api/auth/resend-verification").json()["message"]
    token = last_link(env, "new@test.ph", r"verify\?token=([\w-]+)")
    r = c.get(f"/api/auth/verify?token={token}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/seeker.html?verified=1"
    assert c.get("/api/auth/me").json()["user"]["email_verified"] is True
    assert c.get(f"/api/auth/verify?token={token}", follow_redirects=False).headers["location"].endswith("expired")

    e2 = signup("employer", "unverified@co.test", verify=False)
    e2.post("/api/employer/company", json={"name": "Unverified Co", "industry": "Other"})
    body = dict(title="Clerk", location="Cebu", setup="On-site", level="Entry level", category="Operations",
                description="Keep records tidy and answer phone calls for our Cebu office team.")
    assert e2.post("/api/employer/jobs", json={**body, "action": "publish"}).status_code == 403
    assert e2.post("/api/employer/jobs", json={**body, "action": "draft"}).status_code == 201


def test_password_reset_confirms_email(env):
    c = signup("seeker", "forgot@test.ph", verify=False)
    client().post("/api/auth/forgot", json={"email": "forgot@test.ph"})
    token = last_link(env, "forgot@test.ph", r"reset\.html\?token=([\w-]+)")
    r = client().post("/api/auth/reset", json={"token": token, "password": "newpassword1"})
    assert r.json()["email_verified"] is True


def test_anonymous_alerts_need_confirmation(env):
    from app.alerts import send_alerts
    from app.models import AlertSubscription
    employer_with_job(env)
    assert client().post("/api/alerts", json={"email": "fan@test.ph", "category": "Customer Support"}).status_code == 201
    with env() as s:
        a = s.scalar(select(AlertSubscription).where(AlertSubscription.email == "fan@test.ph"))
        assert a.confirmed is False
        a.last_sent_at = utcnow() - timedelta(days=1)
        s.commit()
    assert send_alerts(env, "daily") == 0                       # not confirmed: nothing sent
    token = last_link(env, "fan@test.ph", r"confirm\?token=([\w-]+)")
    assert "Alert confirmed" in client().get(f"/api/alerts/confirm?token={token}").text
    with env() as s:
        a = s.scalar(select(AlertSubscription).where(AlertSubscription.email == "fan@test.ph"))
        a.last_sent_at = utcnow() - timedelta(days=1)
        s.commit()
    assert send_alerts(env, "daily") == 1


# ---------------------------------------------------------------------------
# Push notifications
# ---------------------------------------------------------------------------

def test_push_subscription_and_delivery(env, setting, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    import http_ece

    pub, priv = push.generate_keys()
    setting("vapid_public_key", pub)
    setting("vapid_private_key", priv)
    setting("vapid_subject", "mailto:test@sinag.test")
    assert client().get("/api/push/key").json() == {"enabled": True, "public_key": pub}

    # A pretend browser: its own key pair and auth secret, like PushManager.subscribe() makes.
    browser_key = ec.generate_private_key(ec.SECP256R1())
    p256dh = base64.urlsafe_b64encode(browser_key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)).decode().rstrip("=")
    auth_secret = os.urandom(16)
    sub = {"endpoint": "https://push.example.test/abc", "keys": {"p256dh": p256dh,
           "auth": base64.urlsafe_b64encode(auth_secret).decode().rstrip("=")}}

    c, _ = seeker_ready()
    assert c.post("/api/me/push", json={**sub, "endpoint": "http://insecure"}).status_code == 422
    assert c.post("/api/me/push", json=sub).status_code == 201

    sent = []

    class Resp:
        def __init__(self, code): self.status_code, self.text, self.content, self.headers, self.reason = code, "", b"", {}, "Gone"

    def fake_post(url, data=None, headers=None, timeout=None, **kw):
        sent.append((url, data, headers))
        return Resp(201)
    monkeypatch.setattr("requests.post", fake_post)

    with env() as s:
        uid = s.scalar(select(PushSubscription.user_id))
        s.add(Notification(user_id=uid, text="Your application was shortlisted", link="/seeker.html#applications"))
        s.commit()
        assert s.scalar(select(PushMessage).where(PushMessage.user_id == uid)) is not None
    assert push.flush_push(env) == 1
    url, data, headers = sent[0]
    assert url == sub["endpoint"] and headers["Content-Encoding"] == "aes128gcm" and "vapid" in headers["Authorization"].lower()
    plain = json.loads(http_ece.decrypt(data, private_key=browser_key, auth_secret=auth_secret, version="aes128gcm"))
    assert plain == {"title": "Sinag Jobs", "body": "Your application was shortlisted", "url": "/seeker.html#applications"}

    # The browser unsubscribed: the push service answers 410 and we forget the subscription.
    monkeypatch.setattr("requests.post", lambda *a, **k: Resp(410))
    with env() as s:
        s.add(Notification(user_id=uid, text="Another update"))
        s.commit()
    push.flush_push(env)
    with env() as s:
        assert s.scalar(select(PushSubscription)) is None


# ---------------------------------------------------------------------------
# Team members
# ---------------------------------------------------------------------------

def test_team_invite_accept_and_remove(env):
    owner, job = employer_with_job(env)
    assert owner.post("/api/employer/team/invite", json={"email": "Recruiter@Acme.test"}).status_code == 201
    token = last_link(env, "recruiter@acme.test", r"join\.html\?token=([\w-]+)")
    assert client().get(f"/api/employer/team/invite-info?token={token}").json()["company"] == "Acme PH"

    wrong = signup("employer", "someone@else.test")
    assert wrong.post("/api/employer/team/accept", json={"token": token}).status_code == 403
    rec = signup("employer", "recruiter@acme.test", "Rhea Recruiter", verify=False)
    assert rec.post("/api/employer/team/accept", json={"token": token}).json()["company"] == "Acme PH"
    assert rec.get("/api/auth/me").json()["user"]["email_verified"] is True
    assert rec.post("/api/employer/team/accept", json={"token": token}).status_code == 400   # used up

    assert rec.get(f"/api/employer/jobs/{job['id']}/applications").status_code == 200       # recruiters can hire
    assert rec.post("/api/employer/team/invite", json={"email": "x@acme.test"}).status_code == 403
    team = owner.get("/api/employer/team").json()
    assert [m["role"] for m in team["members"]] == ["owner", "recruiter"]
    owner.delete(f"/api/employer/team/{team['members'][1]['id']}")
    assert rec.get(f"/api/employer/jobs/{job['id']}/applications").status_code == 409

    seeker = signup("seeker", "js@test.ph")
    assert owner.post("/api/employer/team/invite", json={"email": "js@test.ph"}).status_code == 409


# ---------------------------------------------------------------------------
# Branded company pages
# ---------------------------------------------------------------------------

def test_branding_and_public_page(env):
    e, job = employer_with_job(env)
    bad = e.put("/api/employer/branding", json={"video_url": "https://evil.example/video"})
    assert bad.status_code == 422 and "YouTube or Vimeo" in bad.text
    assert e.put("/api/employer/branding", json={"links": {"facebook": "javascript:alert(1)"}}).status_code == 422
    r = e.put("/api/employer/branding", json={
        "tagline": "Payments for every sari-sari store", "size": "51-200", "founded": 2019,
        "video_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "culture_values": [{"title": "Malasakit", "text": "We care for customers like family."}],
        "testimonials": [{"name": "Liza", "role": "Support lead", "quote": "I grew from agent to lead in two years."}],
        "links": {"linkedin": "https://linkedin.com/company/acme"}})
    assert r.status_code == 200 and r.json()["video_embed"] == "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"
    logo = e.post("/api/employer/branding/images", files={"file": ("logo.png", PNG, "image/png")}, data={"kind": "logo"}).json()["logo_url"]
    e.post("/api/employer/branding/images", files={"file": ("team.png", PNG, "image/png")}, data={"kind": "photo"})
    assert e.post("/api/employer/branding/images", files={"file": ("cv.pdf", PDF, "application/pdf")}, data={"kind": "photo"}).status_code == 422

    page = client().get("/api/companies/acme-ph").json()
    assert page["tagline"].startswith("Payments") and page["culture_values"][0]["title"] == "Malasakit"
    assert page["jobs"][0]["id"] == job["id"] and len(page["photos"]) == 1
    img = client().get(logo)
    assert img.status_code == 200 and img.headers["content-type"] == "image/png"
    assert client().get("/media/companies/1/..%2F..%2Fsecret.png").status_code == 404
    assert client().get(f"/api/jobs/{job['id']}").json()["has_company_page"] is True

    e.post("/api/employer/branding/images/remove", json={"url": logo})
    assert client().get(logo).status_code == 404
    _, _ = employer_with_job(env, email="p@pending.test", company="Pending Co", approve=False)
    assert client().get("/api/companies/pending-co").status_code == 404


# ---------------------------------------------------------------------------
# Resume database and invites
# ---------------------------------------------------------------------------

def test_resume_database(env):
    e, job = employer_with_job(env)
    c = signup("seeker", "maria@test.ph", "Maria Santos")
    c.put("/api/me/profile", json={"full_name": "Maria Santos", "headline": "Python developer", "skills": ["Python", "Django"],
                                    "years_experience": 4, "location": "Cebu", "phone": "0917 000 0000", "searchable": True})
    c.post("/api/me/documents", files={"file": ("cv.docx", docx("Built Kubernetes deployments for a Cebu fintech"), "x")}, data={"kind": "resume"})
    hidden = signup("seeker", "hidden@test.ph", "Hidden Person")
    hidden.put("/api/me/profile", json={"full_name": "Hidden Person", "headline": "Python developer", "skills": ["Python"]})

    pending, _ = employer_with_job(env, email="p@pending.test", company="Pending Co", approve=False)
    assert pending.get("/api/employer/candidates").status_code == 403
    assert c.get("/api/employer/candidates").status_code == 403

    found = e.get("/api/employer/candidates", params={"skills": "python"}).json()
    assert [x["name"] for x in found["candidates"]] == ["Maria Santos"]           # opted-out profile stays hidden
    assert e.get("/api/employer/candidates", params={"q": "kubernetes"}).json()["total"] == 1   # resume text is searched
    assert e.get("/api/employer/candidates", params={"min_years": 5}).json()["total"] == 0
    uid = found["candidates"][0]["id"]
    profile = e.get(f"/api/employer/candidates/{uid}").json()
    assert "maria@test.ph" not in json.dumps(profile) and "0917" not in json.dumps(profile)   # contact details hidden
    assert e.get(f"/api/employer/candidates/{uid}/resume").status_code == 200

    r = e.post(f"/api/employer/candidates/{uid}/invite", json={"job_id": job["id"], "message": "Your Django work fits us."})
    assert r.status_code == 201
    assert e.post(f"/api/employer/candidates/{uid}/invite", json={"job_id": job["id"]}).status_code == 409
    assert any("Django work" in b for _, b in emails(env, "maria@test.ph"))
    inv = c.get("/api/me/invites").json()
    assert inv[0]["job"]["title"] == "Support Associate" and inv[0]["status"] == "sent"

    doc = c.get("/api/me/documents").json()[0]["id"]
    q1 = c.get(f"/api/jobs/{job['id']}").json()["questions"][0]
    c.post(f"/api/jobs/{job['id']}/apply", json={"document_ids": [doc], "answers": {str(q1["id"]): "Yes"}})
    assert c.get("/api/me/invites").json()[0]["status"] == "applied"

    c.put("/api/me/profile", json={"full_name": "Maria Santos", "headline": "Python developer", "skills": ["Python"], "searchable": False})
    assert e.get(f"/api/employer/candidates/{uid}").status_code == 404


# ---------------------------------------------------------------------------
# Skills tests
# ---------------------------------------------------------------------------

TEST = {"title": "Customer support basics", "time_limit_minutes": 10, "pass_score": 60, "questions": [
    {"text": "A customer is angry about a double charge. First step?", "options": ["Refund silently", "Acknowledge and apologise", "Escalate"], "answer": 1},
    {"text": "What does SLA stand for?", "options": ["Service level agreement", "Sales lead account"], "answer": 0},
    {"text": "Best channel for a long technical fix?", "options": ["Chat", "Email with steps", "SMS"], "answer": 1},
]}


def test_skills_test_flow(env):
    e, job = employer_with_job(env)
    bad = e.post("/api/employer/assessments", json={**TEST, "questions": [{"text": "Pick", "options": ["A", "B"], "answer": 5}]})
    assert bad.status_code == 422
    t = e.post("/api/employer/assessments", json=TEST).json()
    body = {k: job[k] for k in ("title", "location", "setup", "shift", "level", "category", "job_type", "salary_min", "salary_max", "skills", "perks")}
    body.update(description=job["description"], questions=[], action="publish", assessment_id=t["id"], assessment_auto=True)
    assert e.put(f"/api/employer/jobs/{job['id']}", json=body).json()["assessment_auto"] is True

    other, _ = employer_with_job(env, email="o@other.test", company="Other Co")
    assert other.put(f"/api/employer/jobs/{job['id']}", json=body).status_code == 404

    c, doc = seeker_ready()
    c.post(f"/api/jobs/{job['id']}/apply", json={"document_ids": [doc], "answers": {}})
    tests = c.get("/api/me/tests").json()
    assert len(tests) == 1 and tests[0]["status"] == "Not started" and tests[0]["questions"] is None
    att = tests[0]["id"]
    started = c.post(f"/api/me/tests/{att}/start").json()
    assert started["seconds_left"] > 500 and "answer" not in json.dumps(started["questions"])
    c.post(f"/api/me/tests/{att}/answers", json={"answers": [1, None, None], "final": False})
    done = c.post(f"/api/me/tests/{att}/answers", json={"answers": [1, 0, 0], "final": True}).json()
    assert done["score"] == 67 and done["passed"] is True and done["status"] == "Completed"
    assert c.post(f"/api/me/tests/{att}/answers", json={"answers": [1, 0, 1]}).status_code == 400

    app_id = e.get(f"/api/employer/jobs/{job['id']}/applications").json()["applications"][0]
    assert app_id["test"] == {"score": 67, "passed": True, "done": True}
    detail = e.get(f"/api/employer/applications/{app_id['id']}").json()
    assert detail["tests"][0]["review"][2]["chosen"] == 0 and detail["tests"][0]["review"][2]["correct"] == 1
    changed = {**TEST, "questions": TEST["questions"][:2]}
    assert e.put(f"/api/employer/assessments/{t['id']}", json=changed).status_code == 400
    assert e.delete(f"/api/employer/assessments/{t['id']}").status_code == 400


def test_late_test_counts_saved_answers_only(env):
    e, job = employer_with_job(env)
    t = e.post("/api/employer/assessments", json=TEST).json()
    c, doc = seeker_ready()
    q1 = c.get(f"/api/jobs/{job['id']}").json()["questions"][0]
    c.post(f"/api/jobs/{job['id']}/apply", json={"document_ids": [doc], "answers": {str(q1["id"]): "Yes"}})
    app_id = e.get(f"/api/employer/jobs/{job['id']}/applications").json()["applications"][0]["id"]
    att = e.post(f"/api/employer/applications/{app_id}/assessment", json={"assessment_id": t["id"]}).json()["attempt_id"]
    c.post(f"/api/me/tests/{att}/start")
    c.post(f"/api/me/tests/{att}/answers", json={"answers": [1, None, None], "final": False})
    with env() as s:
        a = s.get(AssessmentAttempt, att)
        a.deadline = utcnow() - timedelta(minutes=5)
        s.commit()
    late = c.post(f"/api/me/tests/{att}/answers", json={"answers": [1, 0, 1], "final": True}).json()
    assert late["late"] is True and late["score"] == 33


# ---------------------------------------------------------------------------
# Screening chat and AI
# ---------------------------------------------------------------------------

def _job_with_chat(env, **kw):
    return employer_with_job(env, chat_screening=True, chat_topics=["Notice period", "Can you start in two weeks?"], **kw)


def test_scripted_screening_chat(env):
    e, job = _job_with_chat(env)
    c, doc = seeker_ready()
    q1 = c.get(f"/api/jobs/{job['id']}").json()["questions"][0]
    c.post(f"/api/jobs/{job['id']}/apply", json={"document_ids": [doc], "answers": {str(q1["id"]): "Yes"}})
    app_id = c.get("/api/me/applications").json()[0]["id"]
    chat = c.get(f"/api/me/applications/{app_id}/chat").json()
    assert chat["mode"] == "scripted" and chat["messages"][-1]["text"] == "Could you tell us about your notice period?"
    r = c.post(f"/api/me/applications/{app_id}/chat", json={"text": "30 days"}).json()
    assert r["messages"][-1]["text"] == "Can you start in two weeks?" and r["status"] == "open"
    r = c.post(f"/api/me/applications/{app_id}/chat", json={"text": "Yes, I can"}).json()
    assert r["status"] == "done" and r["summary"] is None                     # the summary is for the employer only
    assert c.post(f"/api/me/applications/{app_id}/chat", json={"text": "more"}).status_code == 400
    eid = e.get(f"/api/employer/jobs/{job['id']}/applications").json()["applications"][0]
    assert eid["chat"] == "done"
    summary = e.get(f"/api/employer/applications/{eid['id']}").json()["chat"]["summary"]
    assert "30 days" in summary and "Yes, I can" in summary
    assert e.post(f"/api/employer/applications/{eid['id']}/ai-summary").status_code == 400   # no API key


def test_ai_screening_chat_and_summary(env, setting, monkeypatch):
    setting("anthropic_api_key", "sk-test")
    setting("anthropic_model", "claude-test-model")
    calls = []
    replies = iter(["Thanks! What's your notice period?", "Great. Can you start in two weeks?",
                    "Perfect, thank you. The team will review your application. [DONE]",
                    "- Notice period: 30 days\n- Can start in two weeks: yes",
                    "- Strong Zendesk experience\n- Ask about night shift"])

    def handler(request: httpx.Request):
        calls.append((dict(request.headers), json.loads(request.content)))
        return httpx.Response(200, json={"content": [{"type": "text", "text": next(replies)}]})
    monkeypatch.setattr(ai, "_transport", httpx.MockTransport(handler))

    e, job = _job_with_chat(env)
    c, doc = seeker_ready()
    q1 = c.get(f"/api/jobs/{job['id']}").json()["questions"][0]
    c.post(f"/api/jobs/{job['id']}/apply", json={"document_ids": [doc], "answers": {str(q1["id"]): "Yes"}})
    app_id = c.get("/api/me/applications").json()[0]["id"]
    assert c.get(f"/api/me/applications/{app_id}/chat").json()["mode"] == "ai"
    c.post(f"/api/me/applications/{app_id}/chat", json={"text": "Hi!"})
    c.post(f"/api/me/applications/{app_id}/chat", json={"text": "30 days"})
    r = c.post(f"/api/me/applications/{app_id}/chat", json={"text": "Yes"}).json()
    assert r["status"] == "done" and "[DONE]" not in r["messages"][-1]["text"]
    headers, body = calls[0]
    assert headers["x-api-key"] == "sk-test" and headers["anthropic-version"] == "2023-06-01"
    assert body["model"] == "claude-test-model" and body["messages"][0]["role"] == "user"
    assert "marital" in body["system"]                                    # fairness rules are always sent
    roles = [m["role"] for m in calls[2][1]["messages"]]
    assert all(a != b for a, b in zip(roles, roles[1:]))                  # roles alternate as the API requires

    eapp = e.get(f"/api/employer/jobs/{job['id']}/applications").json()["applications"][0]["id"]
    assert "30 days" in e.get(f"/api/employer/applications/{eapp}").json()["chat"]["summary"]
    s1 = e.post(f"/api/employer/applications/{eapp}/ai-summary").json()["summary"]
    assert "Zendesk" in s1
    assert e.post(f"/api/employer/applications/{eapp}/ai-summary").json()["summary"] == s1   # cached, no new call
    assert len(calls) == 5


def test_ai_failure_falls_back_to_script(env, setting, monkeypatch):
    setting("anthropic_api_key", "sk-test")
    monkeypatch.setattr(ai, "_transport", httpx.MockTransport(lambda r: httpx.Response(500, json={})))
    e, job = _job_with_chat(env)
    c, doc = seeker_ready()
    q1 = c.get(f"/api/jobs/{job['id']}").json()["questions"][0]
    c.post(f"/api/jobs/{job['id']}/apply", json={"document_ids": [doc], "answers": {str(q1["id"]): "Yes"}})
    app_id = c.get("/api/me/applications").json()[0]["id"]
    r = c.post(f"/api/me/applications/{app_id}/chat", json={"text": "30 days"}).json()
    assert r["mode"] == "scripted" and r["messages"][-1]["text"] == "Can you start in two weeks?"
    assert c.post(f"/api/me/applications/{app_id}/chat", json={"text": "Yes"}).json()["status"] == "done"
