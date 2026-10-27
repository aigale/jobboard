import asyncio
import dataclasses

import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.crawler import sync_feed_jobs
from app.demo import FIXTURES
from app.models import Company, Job
from app.normalize import normalize
from app.sources import SOURCES, CompanySpec, Http, RawJob

CASES = {
    "himalayas": ("himalayas_sample.json", 2),
    "arbeitnow": ("arbeitnow_sample.json", 1),
    "remoteok": ("remoteok_sample.json", 2),
    "jobicy": ("jobicy_sample.json", 2),
    "remotive": ("remotive_sample.json", 2),
    "workingnomads": ("workingnomads_sample.json", 2),
    "weworkremotely": ("weworkremotely_sample.xml", 2),
}


def _transport(fixture: str) -> httpx.MockTransport:
    body = (FIXTURES / fixture).read_text(encoding="utf-8")
    ctype = "application/rss+xml" if fixture.endswith(".xml") else "application/json"
    return httpx.MockTransport(lambda request: httpx.Response(200, text=body, headers={"content-type": ctype}))


def fetch(source: str, fixture: str, options: dict | None = None) -> list[RawJob]:
    async def run():
        async with httpx.AsyncClient(transport=_transport(fixture)) as client:
            http = Http(client, per_host_delay=0)
            spec = CompanySpec("feed", "Feed", source, "all", options=options or {})
            return await SOURCES[source](spec, http)

    return asyncio.run(run())


@pytest.mark.parametrize("source", CASES)
def test_feed_sources_parse_employer_and_link(source):
    fixture, expected = CASES[source]
    raws = fetch(source, fixture)
    assert len(raws) == expected
    assert all(raw.company_name for raw in raws)
    assert all(raw.url for raw in raws)
    assert all(raw.remote for raw in raws)


def test_feed_sources_keep_worldwide_roles_without_changing_global_policy():
    cfg = dataclasses.replace(settings, include_worldwide_remote=False)
    for source in ("himalayas", "jobicy", "remoteok", "remotive", "workingnomads", "weworkremotely", "arbeitnow"):
        fixture, _ = CASES[source]
        kept = [n for n in (normalize(raw, cfg) for raw in fetch(source, fixture)) if n]
        assert kept, f"{source} kept no jobs from its sample"
        assert all(n.ph_eligibility in {"ph_remote", "apac_remote", "worldwide_remote"} for n in kept)


def test_feed_jobs_use_real_employers_and_standard_lifecycle(factory, cfg):
    with factory() as session:
        feed = Company(slug="jobicy", name="Jobicy", source="jobicy", source_key="philippines",
                       active=True, approval="approved")
        session.add(feed)
        session.commit()

        keep = RawJob(external_id="9001", title="Customer Support Specialist", url="https://example.com/jobs/9001",
                      company_name="Acme PH", company_logo="https://example.com/acme.png",
                      location="Philippines", locations=["Philippines"], remote=True, workplace_type="remote",
                      description_html="<p>Help customers in the Philippines.</p>")
        skip = RawJob(external_id="9002", title="US Only Role", url="https://example.com/jobs/9002",
                      company_name="Acme US", location="United States", locations=["United States"],
                      remote=True, workplace_type="remote", description_html="<p>US residents only.</p>")

        result = sync_feed_jobs(session, feed, [keep, skip], cfg)
        session.commit()
        assert (result.new, result.seen, result.skipped_not_ph) == (1, 1, 1)

        job = session.scalar(select(Job))
        assert job.external_id == "jobicy:9001"
        assert job.company.name == "Acme PH"
        assert job.company.source == "feed"
        assert job.company.verified is False
        assert job.company.logo_url == "https://example.com/acme.png"
        assert job.ph_eligibility == "ph_remote"

        again = sync_feed_jobs(session, feed, [keep], cfg)
        session.commit()
        assert (again.new, again.updated, again.closed) == (0, 0, 0)

        for _ in range(cfg.close_after_missed_crawls):
            sync_feed_jobs(session, feed, [], cfg)
            session.commit()
        session.refresh(job)
        assert job.status == "closed"

        reopened = sync_feed_jobs(session, feed, [keep], cfg)
        session.commit()
        session.refresh(job)
        assert job.status == "open"
        assert reopened.updated == 1
