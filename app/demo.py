"""Offline demo: crawl recorded sample responses in fixtures/ instead of the internet.

`python -m app.cli demo` fills the database so you can try the site and API without
configuring real companies. Tests use the same transport.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from .config import ROOT, Settings, settings
from .crawler import crawl
from .db import SessionLocal
from .models import Company

FIXTURES = ROOT / "fixtures"

DEMO_COMPANIES = [
    dict(slug="kalesa-labs", name="Kalesa Labs", source="greenhouse", source_key="kalesalabs", industry="Software & Technology"),
    dict(slug="southwind-saas", name="Southwind SaaS", source="lever", source_key="southwind", industry="Software & Technology"),
    dict(slug="bayani-pay", name="Bayani Pay", source="ashby", source_key="bayanipay", industry="Banking & Finance"),
    dict(slug="harbor-and-pine", name="Harbor & Pine Realty", source="workable", source_key="harbor-and-pine", industry="Construction & Real Estate"),
    dict(slug="visayas-medical", name="Visayas Medical Center", source="smartrecruiters", source_key="VisayasMedical", industry="Healthcare"),
    dict(slug="tindahan", name="Tindahan.co", source="jsonld", source_key="https://tindahan.example.ph/careers", industry="E-commerce & Retail",
         options={"job_link_pattern": r"/careers/[a-z0-9-]+$"}),
]


def _fill_dates(text: str, now: datetime) -> str:
    text = re.sub(r"\{\{ago:(\d+)\}\}", lambda m: (now - timedelta(minutes=int(m.group(1)))).isoformat(), text)
    text = re.sub(r'"\{\{ago_ms:(\d+)\}\}"',
                  lambda m: str(int((now - timedelta(minutes=int(m.group(1)))).timestamp() * 1000)), text)
    text = re.sub(r"\{\{date:(\d+)\}\}", lambda m: (now - timedelta(days=int(m.group(1)))).date().isoformat(), text)
    return text


def fixture_transport(fixtures: Path = FIXTURES, now: datetime | None = None,
                      overrides: dict[str, str | None] | None = None) -> httpx.MockTransport:
    """Serve fixture files for the URLs each source requests.
    `overrides` maps a fixture filename to replacement text (or None to answer 503)."""
    now = now or datetime.now(timezone.utc)
    overrides = overrides or {}

    def read(name: str) -> str | None:
        if name in overrides:
            return overrides[name]
        path = fixtures / name
        return _fill_dates(path.read_text(), now) if path.exists() else ""

    def handler(request: httpx.Request) -> httpx.Response:
        u = urlsplit(str(request.url))
        host, path = u.netloc, u.path.rstrip("/")
        name = None
        if host == "boards-api.greenhouse.io":
            name = f"greenhouse_{path.split('/')[3]}.json"
        elif host in ("api.lever.co", "api.eu.lever.co"):
            name = f"lever_{path.split('/')[-1]}.json"
        elif host == "api.ashbyhq.com":
            name = f"ashby_{path.split('/')[-1]}.json"
        elif host == "apply.workable.com":
            name = f"workable_{path.split('/')[-1]}.json"
        elif host == "api.smartrecruiters.com":
            parts = path.split("/")          # /v1/companies/<co>/postings[/<id>]
            name = f"smartrecruiters_{parts[3]}_{parts[5] if len(parts) > 5 else 'list'}.json"
        elif host == "tindahan.example.ph":
            if path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nDisallow: /admin\n")
            slug = path.split("/")[-1]
            name = "jsonld_tindahan_listing.html" if slug == "careers" else f"jsonld_tindahan_{slug}.html"
        if name is None:
            return httpx.Response(404)
        body = read(name)
        if body is None:
            return httpx.Response(503, text="temporarily unavailable")
        if body == "":
            return httpx.Response(404)
        ctype = "application/json" if name.endswith(".json") else "text/html; charset=utf-8"
        return httpx.Response(200, text=body, headers={"content-type": ctype})

    return httpx.MockTransport(handler)


def ensure_demo_companies(session) -> None:
    for d in DEMO_COMPANIES:
        c = session.scalar(select(Company).where(Company.slug == d["slug"]))
        if c is None:
            c = Company(slug=d["slug"])
            session.add(c)
        c.name, c.source, c.source_key = d["name"], d["source"], d["source_key"]
        c.options = d.get("options", {})
        c.industry = d.get("industry")
        c.active = True
    session.commit()


def load_demo(cfg: Settings = settings):
    with SessionLocal() as s:
        ensure_demo_companies(s)
    fast = dataclasses.replace(cfg, per_host_delay_seconds=0.0)
    return asyncio.run(crawl(SessionLocal, fast, transport=fixture_transport()))


DEMO_PASSWORD = "sinag-demo-123"

DEMO_DIRECT_JOBS = [
    dict(title="Customer Success Associate", location="Cebu", city="Cebu City", setup="Hybrid", shift="Day shift",
         level="Entry level", category="Customer Support", job_type="Full-time", salary_min=25000, salary_max=32000,
         skills=["Customer service", "English", "Zendesk"], perks=["HMO day 1", "13th month", "Rice allowance"],
         description="Help small Philippine businesses get the most out of our payments app.\n\n"
                     "- Answer chats and emails from merchants\n- Walk new customers through setup\n"
                     "- Report bugs to the product team\n\nFresh graduates are welcome. Paid training for 2 weeks.",
         questions=[dict(text="Are you willing to report to our Cebu IT Park office 3 days a week?", kind="yesno",
                         required=True, knockout_answer="Yes"),
                    dict(text="Tell us about a time you helped an upset customer.", kind="text", required=True)]),
    dict(title="Python Backend Developer", location="Remote (PH-based)", setup="Remote", shift="Day shift",
         level="Mid level", category="Engineering", job_type="Full-time", salary_min=70000, salary_max=95000,
         skills=["Python", "FastAPI", "PostgreSQL", "Docker"], perks=["HMO day 1", "13th month", "Equipment provided"],
         description="Build the APIs behind Sinag Demo Corp's merchant tools. You'll work with a team of 6 "
                     "engineers in Manila and Cebu.\n\n- 3+ years with Python web frameworks\n"
                     "- Comfortable with SQL and writing tests\n- Docker experience is a plus",
         questions=[dict(text="How many years have you worked with Python?", kind="choice",
                         options=["Less than 1", "1-2", "3-5", "More than 5"], required=True),
                    dict(text="Link to your GitHub or portfolio (optional)", kind="text", required=False)]),
    dict(title="Bookkeeper (Part-time)", location="Metro Manila", city="Makati", setup="On-site", shift="Day shift",
         level="Mid level", category="Finance", job_type="Part-time", salary_min=18000, salary_max=22000,
         skills=["Bookkeeping", "Xero", "Excel"], perks=["Paid leave"],
         description="Keep the books for our Makati office, 4 hours a day, Monday to Friday.\n\n"
                     "- Record payables and receivables\n- Prepare monthly BIR forms with our CPA",
         questions=[]),
]


def load_demo_accounts() -> list[str]:
    """Demo employer (approved company + 3 Easy Apply jobs), job seeker and admin. Local use only."""
    from .auth import hash_password
    from .employer import JobIn, _apply_job_fields, _publish
    from .models import CandidateProfile, CompanyMember, Job, User, join_pipes, utcnow
    import secrets as _secrets

    with SessionLocal() as s:
        def user(email, name, role):
            u = s.scalar(select(User).where(User.email == email))
            if u is None:
                u = User(email=email, name=name, role=role, password_hash=hash_password(DEMO_PASSWORD), consent_at=utcnow(),
                         email_verified_at=utcnow())
                s.add(u)
                s.flush()
            return u

        emp = user("employer@demo.sinag", "Maria Santos", "employer")
        seeker = user("seeker@demo.sinag", "Juan Dela Cruz", "seeker")
        user("admin@demo.sinag", "Demo Admin", "admin")
        if s.get(CandidateProfile, seeker.id) is None:
            s.add(CandidateProfile(user_id=seeker.id, full_name="Juan Dela Cruz", headline="Customer support specialist",
                                   location="Cebu", skills=join_pipes(["Customer service", "English", "Zendesk", "Excel"]),
                                   years_experience=1, desired_salary=25000, desired_setups=join_pipes(["Hybrid", "Remote"]),
                                   desired_categories=join_pipes(["Customer Support"]), searchable=True,
                                   summary="One year handling chat and email support for a Cebu fintech. Calm with upset customers."))
        c = s.scalar(select(Company).where(Company.slug == "sinag-demo-corp"))
        if c is None:
            c = Company(slug="sinag-demo-corp", name="Sinag Demo Corp", source="direct", source_key="",
                        industry="Software & Technology", website="https://example.com", location_text="Cebu City",
                        about="A demo employer for trying Easy Apply and the applicant board.", approval="approved",
                        verified=True, active=True, options={})
            s.add(c)
            s.flush()
            s.add(CompanyMember(company_id=c.id, user_id=emp.id, role="owner"))
            c.tagline = "Simple payment tools for small Philippine businesses"
            c.size, c.founded = "51-200", 2019
            c.culture_values = [{"title": "Malasakit", "text": "We treat every merchant's problem like our own."},
                                {"title": "Bayanihan", "text": "Nobody ships alone. We help each other finish."},
                                {"title": "Tapat", "text": "We say what we know and what we don't."}]
            c.testimonials = [{"name": "Liza M.", "role": "Support lead", "quote": "I started as an agent in 2021 and now lead a team of eight. The training is real."}]
            c.links = {"linkedin": "https://www.linkedin.com/"}
            from .models import Assessment
            test = Assessment(company_id=c.id, title="Customer support basics", time_limit_minutes=10, pass_score=60,
                              instructions="Five questions about handling customers by chat and email. No outside help, please.",
                              questions=[
                                  {"text": "A customer was charged twice. What do you do first?", "options": ["Refund right away without replying", "Acknowledge the problem and apologise", "Ask them to call the bank"], "answer": 1},
                                  {"text": "What does SLA mean?", "options": ["Service level agreement", "Sales lead account", "Support log archive"], "answer": 0},
                                  {"text": "Best channel for long step-by-step instructions?", "options": ["Chat", "Email", "SMS"], "answer": 1},
                                  {"text": "A customer writes in Tagalog. You should…", "options": ["Reply in English only", "Reply in Tagalog or Taglish if you can", "Transfer the ticket"], "answer": 1},
                                  {"text": "When should you close a ticket?", "options": ["Right after replying", "When the customer confirms it's solved", "After 24 hours"], "answer": 1}])
            s.add(test)
            s.flush()
            now = utcnow()
            for spec in DEMO_DIRECT_JOBS:
                extra = {}
                if spec["title"].startswith("Customer Success"):
                    extra = dict(assessment_id=test.id, assessment_auto=True)
                if spec["title"].startswith("Python"):
                    extra = dict(chat_screening=True, chat_topics=["Notice period", "Are you comfortable with a 2-hour weekly sync in Manila time?"])
                body = JobIn(**spec, action="publish", **extra)
                job = Job(company_id=c.id, external_id=f"direct-{_secrets.token_hex(8)}", origin="direct",
                          ph_eligibility="ph_location", easy_apply=True, created_by_id=emp.id, posted_at=now,
                          first_seen_at=now, last_seen_at=now, url="", status="draft")
                s.add(job)
                _apply_job_fields(job, body)
                _publish(job, body)
                s.flush()
                from .common import job_path
                job.paid_via = "admin"
                job.url = job.apply_url = settings.public_url + job_path(job)
        s.commit()
        _demo_stats(s, c)
    return ["employer@demo.sinag  (Sinag Demo Corp, 3 Easy Apply jobs)",
            "seeker@demo.sinag    (profile filled in; upload a resume to apply)",
            "admin@demo.sinag     (approve employers at /admin.html)",
            "Sample analytics for the last 30 days (made-up numbers, for trying the charts)"]


def _demo_stats(s, company) -> None:
    """Made-up daily counters so the analytics charts have something to show locally. Runs once."""
    import random
    from datetime import date, timedelta
    from .analytics import today
    from .models import DailyStat, Job
    if s.scalar(select(DailyStat.id).where(DailyStat.metric == "visit").limit(1)):
        return
    rnd = random.Random(7)
    end = date.fromisoformat(today())
    own = {str(j) for j in s.scalars(select(Job.id).where(Job.company_id == company.id))}
    job_ids = sorted(own) + [str(j) for j in s.scalars(select(Job.id).where(Job.status == "open", Job.company_id != company.id)
                                                       .order_by(Job.id).limit(10))]
    searches = ["virtual assistant", "customer service", "python", "accountant", "nurse", "graphic designer", "data entry", "cebu"]
    for i in range(30, -1, -1):          # i == 0 is today, so far
        day = (end - timedelta(days=i)).isoformat()
        weekend = (end - timedelta(days=i)).weekday() >= 5
        base = int((260 + (30 - i) * 6) * (0.65 if weekend else 1) * rnd.uniform(0.85, 1.15) * (0.55 if i == 0 else 1))
        uniq = int(base * rnd.uniform(0.58, 0.7))
        rows = [("visit", "", base), ("unique", "", uniq), ("signup", "seeker", max(0, int(uniq * rnd.uniform(0.01, 0.03)))),
                ("signup", "employer", rnd.choice([0, 0, 0, 1]))]
        for j in job_ids:
            v = int(rnd.uniform(4, 30) * (1.6 if j in own else 1))
            rows.append(("job_view", j, v))
            if j in own:
                rows.append(("application", j, int(v * rnd.uniform(0.03, 0.12))))
            else:
                rows.append(("apply_click", j, int(v * rnd.uniform(0.05, 0.15))))
        for q in rnd.sample(searches, 4):
            rows.append(("search", q, rnd.randint(2, 18)))
        for metric, key, n in rows:
            if n:
                s.add(DailyStat(day=day, metric=metric, key=key, count=n))
    s.commit()
