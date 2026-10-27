"""Crawl every active company, keep Philippine jobs, and sync them into the database.

Rules that keep the board trustworthy:
- A company whose source fails this round keeps all its jobs as they are (no false closures).
- A job is closed only after it is missing from `close_after_missed_crawls` successful crawls in a row.
- A closed job that reappears is reopened with its original id, so links keep working.
- Jobs not open to people in the Philippines are skipped and counted.
"""
from __future__ import annotations

import asyncio
import hashlib
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
from .sources import FEED_SOURCES, SOURCES, CompanySpec, Http, RawJob, SourceError

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


def _prepare(raws: list[RawJob], cfg: Settings, res: CompanyResult) -> dict[str, tuple[NormalizedJob, RawJob]]:
    fresh: dict[str, tuple[NormalizedJob, RawJob]] = {}
    for raw in raws:
        if not raw.external_id or not raw.title or not raw.url:
            continue
        n = normalize(raw, cfg)
        if n is None:
            res.skipped_not_ph += 1
            continue
        fresh.setdefault(n.external_id, (n, raw))
    return fresh


def _save_job(session: Session, job: Job | None, company_id: int, external_id: str, n: NormalizedJob,
              now: datetime, res: CompanyResult) -> None:
    if job is None:
        job = Job(company_id=company_id, external_id=external_id, first_seen_at=now, status="open", origin="crawled",
                  posted_at=min(n.posted_at, now))
        _apply(job, n)
        job.last_seen_at = now
        job.missed_crawls = 0
        session.add(job)
        res.new += 1
        return
    if job.status == "closed":
        job.status, job.closed_at = "open", None
        res.updated += 1
    elif job.content_hash != n.content_hash:
        res.updated += 1
    _apply(job, n)
    job.last_seen_at = now
    job.missed_crawls = 0


def _close_missing(existing: dict[str, Job], fresh_ids: set[str], cfg: Settings, now: datetime,
                   res: CompanyResult) -> None:
    for ext_id, job in existing.items():
        if ext_id in fresh_ids or job.status != "open":
            continue
        job.missed_crawls = (job.missed_crawls or 0) + 1
        if job.missed_crawls >= cfg.close_after_missed_crawls:
            job.status, job.closed_at = "closed", now
            res.closed += 1


def _mark_crawled(company: Company, now: datetime) -> None:
    company.last_crawled_at = now
    company.last_status = "ok"
    company.last_error = None
    company.consecutive_failures = 0


def sync_company_jobs(session: Session, company: Company, raws: list[RawJob],
                      cfg: Settings, now: datetime | None = None) -> CompanyResult:
    now = now or datetime.now(timezone.utc)
    res = CompanyResult()
    fresh = _prepare(raws, cfg, res)
    existing = {j.external_id: j for j in session.scalars(
        select(Job).where(Job.company_id == company.id, Job.origin == "crawled"))}

    for ext_id, (n, _) in fresh.items():
        res.seen += 1
        _save_job(session, existing.get(ext_id), company.id, ext_id, n, now, res)

    _close_missing(existing, set(fresh), cfg, now, res)
    _mark_crawled(company, now)
    return res


def _feed_external_id(prefix: str, external_id: str) -> str:
    """Keep feed ids inside Job.external_id's 200-char column without losing uniqueness."""
    key = prefix + external_id
    if len(key) <= 200:
        return key
    digest = hashlib.sha1(external_id.encode()).hexdigest()[:20]
    return f"{prefix}{digest}"


def _feed_child_company(session: Session, feed: Company, raw: RawJob) -> Company:
    """Find or create the employer behind one job in an aggregator feed.

    Feed jobs are attributed to the real employer instead of the job board. Existing
    employer pages (from a direct ATS crawl) are reused when the names match.
    """
    name = " ".join((raw.company_name or "").split())[:200] or "Unknown employer"
    slug = slugify(name)[:100] or f"{feed.slug}-employer"
    company = session.scalar(select(Company).where(Company.slug == slug))
    # Never attach feed jobs to the feed's own row (or to an employer account).
    if company is not None and (company.source == "direct" or company.source in FEED_SOURCES):
        slug = f"{slug}-remote"[:100]
        company = session.scalar(select(Company).where(Company.slug == slug))
    if company is None:
        company = Company(slug=slug, name=name, source="feed", source_key=f"{feed.slug}:{slug}",
                          options={"feed": feed.slug}, active=True, approval="approved", verified=False)
        session.add(company)
        session.flush()
    elif company.source == "feed":
        company.name = name
        company.options = {**(company.options or {}), "feed": feed.slug}
    if raw.company_logo and not company.logo_url:
        company.logo_url = raw.company_logo
    return company


def sync_feed_jobs(session: Session, feed: Company, raws: list[RawJob],
                   cfg: Settings, now: datetime | None = None) -> CompanyResult:
    """Sync an aggregator feed whose jobs belong to many different employers.

    `external_id` carries a feed prefix so two feeds can list the same employer (or the
    same job) without colliding, and so one feed's lifecycle never closes another's jobs.
    """
    now = now or datetime.now(timezone.utc)
    res = CompanyResult()
    prefix = f"{feed.slug}:"
    fresh = _prepare(raws, cfg, res)
    keys = {ext_id: _feed_external_id(prefix, ext_id) for ext_id in fresh}
    fresh_ids = set(keys.values())
    existing = {j.external_id: j for j in session.scalars(
        select(Job).where(Job.origin == "crawled", Job.external_id.like(prefix + "%")))}

    for ext_id, (n, raw) in fresh.items():
        res.seen += 1
        key = keys[ext_id]
        job = existing.get(key)
        if job is None:
            employer = _feed_child_company(session, feed, raw)
            employer.last_crawled_at = now
            employer.last_status = "ok"
            _save_job(session, None, employer.id, key, n, now, res)
        else:
            _save_job(session, job, job.company_id, key, n, now, res)

    _close_missing(existing, fresh_ids, cfg, now, res)
    _mark_crawled(feed, now)
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
        headers={"User-Agent": cfg.user_agent,
                 "Accept": "application/json, text/html;q=0.9, application/rss+xml;q=0.9, text/xml;q=0.8"},
        timeout=cfg.http_timeout_seconds, transport=transport,
        limits=httpx.Limits(max_connections=cfg.crawl_concurrency * 2),
    )


async def crawl(session_factory: sessionmaker = SessionLocal, cfg: Settings = default_settings, *,
                only_slug: str | None = None, transport: httpx.AsyncBaseTransport | None = None) -> CrawlRun:
    with session_factory() as session:
        # "feed" companies are employers discovered inside an aggregator feed, not
        # sources of their own; they must never be fetched directly.
        q = select(Company).where(Company.active.is_(True), Company.source.notin_(("direct", "feed")))
        if only_slug:
            q = select(Company).where(Company.slug == only_slug, Company.source.notin_(("direct", "feed")))
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
                if company.source in FEED_SOURCES:
                    r = sync_feed_jobs(session, company, f.raws, cfg)
                else:
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
