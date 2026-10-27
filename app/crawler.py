"""Crawl every active company, keep Philippine jobs, and sync them into the database.

Rules that keep the board trustworthy:
- A company whose source fails this round keeps all its jobs as they are (no false closures).
- A job is closed only after it is missing from `close_after_missed_crawls` successful crawls in a row.
- A closed job that reappears is reopened with its original id, so links keep working.
- Jobs not open to people in the Philippines are skipped and counted.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings, settings as default_settings
from .db import SessionLocal
from .models import Company, CrawlRun, Job
from .normalize import NormalizedJob, normalize
from .sources import SOURCES, CompanySpec, Http, RawJob, SourceError

log = logging.getLogger("sinag.crawler")


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


# ---------------------------------------------------------------------------
# Company list
# ---------------------------------------------------------------------------

def sync_companies(session: Session, path: Path) -> tuple[int, int]:
    """Create or update companies from companies.yaml. Returns (created, updated)."""
    if not path.exists():
        raise FileNotFoundError(f"{path} not found. Copy companies.example.yaml to companies.yaml to start.")
    entries = yaml.safe_load(path.read_text()) or []
    created = updated = 0
    for e in entries:
        if not e or "source" not in e or "key" not in e or "name" not in e:
            raise ValueError(f"Each company needs name, source and key: {e!r}")
        if e["source"] not in SOURCES:
            raise ValueError(f"Unknown source {e['source']!r} for {e['name']}. Use one of: {', '.join(SOURCES)}")
        slug = e.get("slug") or slugify(e["name"])
        c = session.scalar(select(Company).where(Company.slug == slug))
        if c is not None and c.source == "direct":
            log.warning("companies.yaml: %r has the same slug as an employer account; add a different `slug:`", e["name"])
            continue
        if c is None:
            c = Company(slug=slug)
            session.add(c)
            created += 1
        else:
            updated += 1
        c.name = e["name"]
        c.source = e["source"]
        c.source_key = str(e["key"])
        c.careers_url = e.get("careers_url")
        c.logo_url = e.get("logo_url")
        c.options = e.get("options") or {}
        c.active = e.get("active", True)
        c.verified = e.get("verified", True)
        c.industry = e.get("industry") or c.industry
        c.approval = "approved"
    session.commit()
    return created, updated


# ---------------------------------------------------------------------------
# Syncing one company's jobs
# ---------------------------------------------------------------------------

@dataclass
class CompanyResult:
    new: int = 0
    updated: int = 0
    closed: int = 0
    seen: int = 0
    skipped_not_ph: int = 0


_FIELDS = ("title", "url", "apply_url", "department", "employment_type", "job_type", "location_raw", "location", "city",
           "ph_eligibility", "setup", "shift", "level", "category", "salary_min", "salary_max", "salary_original",
           "description_html", "description_text", "content_hash")


def _apply(job: Job, n: NormalizedJob) -> None:
    for f in _FIELDS:
        setattr(job, f, getattr(n, f))
    job.perks = "|" + "|".join(n.perks) + "|" if n.perks else ""


def sync_company_jobs(session: Session, company: Company, raws: list[RawJob],
                      cfg: Settings, now: datetime | None = None) -> CompanyResult:
    now = now or datetime.now(timezone.utc)
    res = CompanyResult()

    fresh: dict[str, NormalizedJob] = {}
    for raw in raws:
        if not raw.title or not raw.url:
            continue
        n = normalize(raw, cfg)
        if n is None:
            res.skipped_not_ph += 1
            continue
        fresh.setdefault(n.external_id, n)

    existing = {j.external_id: j for j in session.scalars(
        select(Job).where(Job.company_id == company.id, Job.origin == "crawled"))}

    for ext_id, n in fresh.items():
        res.seen += 1
        job = existing.get(ext_id)
        if job is None:
            job = Job(company_id=company.id, external_id=ext_id, first_seen_at=now, status="open", origin="crawled",
                      posted_at=min(n.posted_at, now))
            _apply(job, n)
            job.last_seen_at = now
            job.missed_crawls = 0
            session.add(job)
            res.new += 1
            continue
        if job.status == "closed":
            job.status, job.closed_at = "open", None
            res.updated += 1
        elif job.content_hash != n.content_hash:
            res.updated += 1
        _apply(job, n)
        job.last_seen_at = now
        job.missed_crawls = 0

    for ext_id, job in existing.items():
        if ext_id in fresh or job.status != "open":
            continue
        job.missed_crawls = (job.missed_crawls or 0) + 1
        if job.missed_crawls >= cfg.close_after_missed_crawls:
            job.status, job.closed_at = "closed", now
            res.closed += 1

    company.last_crawled_at = now
    company.last_status = "ok"
    company.last_error = None
    company.consecutive_failures = 0
    return res


# ---------------------------------------------------------------------------
# Full crawl
# ---------------------------------------------------------------------------

@dataclass
class _Fetched:
    company_id: int
    raws: list[RawJob] = field(default_factory=list)
    error: str | None = None


def build_client(cfg: Settings, transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": cfg.user_agent, "Accept": "application/json, text/html;q=0.9"},
        timeout=cfg.http_timeout_seconds, transport=transport,
        limits=httpx.Limits(max_connections=cfg.crawl_concurrency * 2),
    )


async def crawl(session_factory: sessionmaker = SessionLocal, cfg: Settings = default_settings, *,
                only_slug: str | None = None, transport: httpx.AsyncBaseTransport | None = None) -> CrawlRun:
    with session_factory() as session:
        q = select(Company).where(Company.active.is_(True), Company.source != "direct")
        if only_slug:
            q = select(Company).where(Company.slug == only_slug, Company.source != "direct")
        companies = list(session.scalars(q))
        specs = [(c.id, CompanySpec(c.slug, c.name, c.source, c.source_key, c.careers_url, c.options or {}))
                 for c in companies]
        run = CrawlRun()
        session.add(run)
        session.commit()
        run_id = run.id

    sem = asyncio.Semaphore(cfg.crawl_concurrency)
    async with build_client(cfg, transport) as client:
        http = Http(client, per_host_delay=cfg.per_host_delay_seconds)

        async def one(cid: int, spec: CompanySpec) -> _Fetched:
            async with sem:
                try:
                    raws = await SOURCES[spec.source](spec, http)
                    log.info("%s: %d jobs from %s", spec.slug, len(raws), spec.source)
                    return _Fetched(cid, raws)
                except SourceError as exc:
                    return _Fetched(cid, error=str(exc))
                except Exception as exc:   # a broken source must never stop the whole crawl
                    log.exception("%s failed", spec.slug)
                    return _Fetched(cid, error=f"{type(exc).__name__}: {exc}")

        fetched = await asyncio.gather(*(one(cid, spec) for cid, spec in specs))

    with session_factory() as session:
        run = session.get(CrawlRun, run_id)
        errors = []
        for f in fetched:
            company = session.get(Company, f.company_id)
            if f.error:
                company.last_status = "error"
                company.last_error = f.error[:2000]
                company.last_crawled_at = datetime.now(timezone.utc)
                company.consecutive_failures = (company.consecutive_failures or 0) + 1
                run.companies_failed += 1
                errors.append({"company": company.slug, "error": f.error[:500]})
                log.warning("%s: %s", company.slug, f.error)
                session.commit()
                continue
            try:
                r = sync_company_jobs(session, company, f.raws, cfg)
                session.commit()
            except Exception as exc:
                session.rollback()
                run.companies_failed += 1
                errors.append({"company": company.slug, "error": f"save failed: {exc}"[:500]})
                log.exception("saving %s failed", company.slug)
                continue
            run.companies_ok += 1
            run.jobs_seen += r.seen
            run.jobs_new += r.new
            run.jobs_updated += r.updated
            run.jobs_closed += r.closed
            run.jobs_skipped_not_ph += r.skipped_not_ph
        run.errors = errors
        run.finished_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(run)
        session.expunge(run)
    log.info("crawl done: %d ok, %d failed, %d new, %d updated, %d closed, %d skipped (not PH)",
             run.companies_ok, run.companies_failed, run.jobs_new, run.jobs_updated, run.jobs_closed,
             run.jobs_skipped_not_ph)
    return run
