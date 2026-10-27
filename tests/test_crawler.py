import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app.crawler import crawl, sync_companies
from app.demo import FIXTURES, ensure_demo_companies, fixture_transport
from app.models import Company, Job
from app.sources import SOURCES, CompanySpec, Http, SourceError


def run(factory, cfg, **kw):
    return asyncio.run(crawl(factory, cfg, **kw))


def open_titles(factory, slug=None):
    with factory() as s:
        q = select(Job).join(Company).where(Job.status == "open")
        if slug:
            q = q.where(Company.slug == slug)
        return sorted(j.title for j in s.scalars(q))


def test_every_source_collects_only_philippine_jobs(factory, cfg):
    with factory() as s:
        ensure_demo_companies(s)
    r = run(factory, cfg, transport=fixture_transport())
    assert (r.companies_ok, r.companies_failed, r.jobs_new, r.jobs_skipped_not_ph) == (6, 0, 12, 3)
    titles = open_titles(factory)
    assert "Staff Platform Engineer" not in titles      # San Francisco
    assert "Account Executive" not in titles            # London
    assert "Leasing Agent" not in titles                # Miami
    assert "Registered Nurse" not in titles             # Riyadh (filtered before details are fetched)
    assert "Unlisted test role" not in titles           # hidden on Ashby
    assert open_titles(factory, "tindahan") == ["Operations Associate, E-commerce", "SEO Content Writer"]


def test_job_lifecycle_update_close_reopen(factory, cfg):
    with factory() as s:
        ensure_demo_companies(s)
    run(factory, cfg, transport=fixture_transport())
    with factory() as s:
        original_id = s.scalar(select(Job.id).where(Job.title == "Customer Experience Specialist"))

    gh = json.loads((FIXTURES / "greenhouse_kalesalabs.json").read_text().replace("{{ago:", "{{x:"))
    now = datetime.now(timezone.utc).isoformat()
    for j in gh["jobs"]:
        j["first_published"] = j["updated_at"] = now
    removed = [j for j in gh["jobs"] if j["id"] == 4012]
    gh["jobs"] = [j for j in gh["jobs"] if j["id"] != 4012]
    gh["jobs"][0]["title"] = "Senior React Developer (Payments)"
    without = json.dumps(gh)

    r = run(factory, cfg, transport=fixture_transport(overrides={"greenhouse_kalesalabs.json": without}))
    assert r.jobs_updated == 1 and r.jobs_closed == 0      # missing once: still open
    assert "Customer Experience Specialist" in open_titles(factory)

    r = run(factory, cfg, transport=fixture_transport(overrides={"greenhouse_kalesalabs.json": without}))
    assert r.jobs_closed == 1                               # missing twice: closed
    assert "Customer Experience Specialist" not in open_titles(factory)

    gh["jobs"] += removed
    r = run(factory, cfg, transport=fixture_transport(overrides={"greenhouse_kalesalabs.json": json.dumps(gh)}))
    with factory() as s:
        job = s.scalar(select(Job).where(Job.title == "Customer Experience Specialist"))
        assert job.status == "open" and job.id == original_id and job.closed_at is None


def test_failed_source_keeps_existing_jobs(factory, cfg):
    with factory() as s:
        ensure_demo_companies(s)
    run(factory, cfg, transport=fixture_transport())
    before = open_titles(factory, "kalesa-labs")
    for _ in range(3):
        r = run(factory, cfg, transport=fixture_transport(overrides={"greenhouse_kalesalabs.json": None}))
        assert r.companies_failed == 1 and r.companies_ok == 5 and r.jobs_closed == 0
    assert open_titles(factory, "kalesa-labs") == before
    with factory() as s:
        c = s.scalar(select(Company).where(Company.slug == "kalesa-labs"))
        assert c.last_status == "error" and c.consecutive_failures == 3 and "503" in c.last_error


def test_crawl_is_idempotent(factory, cfg):
    with factory() as s:
        ensure_demo_companies(s)
    now = datetime.now(timezone.utc)
    run(factory, cfg, transport=fixture_transport(now=now))
    r = run(factory, cfg, transport=fixture_transport(now=now))
    assert (r.jobs_new, r.jobs_updated, r.jobs_closed) == (0, 0, 0)


def test_robots_txt_is_respected(cfg):
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /careers\n")
        return httpx.Response(200, text="<html></html>")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            spec = CompanySpec("x", "X", "jsonld", "https://blocked.example/careers")
            await SOURCES["jsonld"](spec, Http(c, per_host_delay=0))
    with pytest.raises(SourceError, match="robots.txt"):
        asyncio.run(go())


def test_retries_then_succeeds(cfg):
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(429, headers={"retry-after": "0"}) if calls["n"] < 3 else httpx.Response(200, json={"jobs": []})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await SOURCES["greenhouse"](CompanySpec("x", "X", "greenhouse", "x"), Http(c, per_host_delay=0))
    assert asyncio.run(go()) == [] and calls["n"] == 3


def test_companies_yaml_validation(factory, tmp_path):
    f = tmp_path / "companies.yaml"
    f.write_text("- {name: Acme PH, source: greenhouse, key: acmeph}\n- {name: Beta, source: lever, key: beta}\n")
    with factory() as s:
        assert sync_companies(s, f) == (2, 0)
        assert sync_companies(s, f) == (0, 2)
        assert s.scalar(select(Company.slug).where(Company.name == "Acme PH")) == "acme-ph"
    f.write_text("- {name: Bad, source: myspace, key: x}\n")
    with factory() as s, pytest.raises(ValueError, match="Unknown source"):
        sync_companies(s, f)
