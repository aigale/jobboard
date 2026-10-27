"""Bulk job imports (backfills) from CSV or XML, as a one-time upload or a feed re-read every day.

CSV: one job per row. Columns (header names are not case-sensitive):
  title*, description*, reference, location, city, setup, job_type, level, category, shift,
  salary_min, salary_max, skills (separated by ; or |), apply_url, expires (YYYY-MM-DD)
XML: the common "Indeed-style" feed: <source><job><title/><referencenumber/><url/><description/>
  <city/><state/><country/><salary/><jobtype/><category/><expirationdate/><remotetype/></job></source>

Missing fields are filled in by the same rules the crawler uses (location, setup, level, category, pay).
Jobs outside the Philippines are skipped. Each job is matched by its reference, so importing the same
file twice updates jobs instead of duplicating them.
"""
from __future__ import annotations

import csv
import hashlib
import io
import logging
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse
from lxml import etree
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import require_admin_user, require_employer
from .common import CATEGORIES, JOB_TYPES, LEVELS, LOCATIONS, SETUPS, SHIFTS, job_path, slug, text_to_html
from .config import settings
from .crawler import slugify
from .db import get_session
from .employer import need_company
from .models import Company, ImportFeed, Job, aware, join_pipes, utcnow
from .normalize import normalize
from .sources.base import RawJob

log = logging.getLogger("sinag.importer")
router = APIRouter(tags=["imports"])
MAX_BYTES = 10 * 1024 * 1024
MAX_ROWS = 5000
TEMPLATE = ("title,description,reference,location,city,setup,job_type,level,category,shift,salary_min,salary_max,skills,apply_url,expires\n"
            "Customer Support Associate,\"Answer customer chats and emails for a US retailer. Night shift, HMO from day 1.\",CSA-001,"
            "Cebu,Cebu City,On-site,Full-time,Entry level,Customer Support,Night shift,25000,30000,English;Zendesk,,2026-12-31\n")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _pick(value: str | None, allowed: list[str]) -> str | None:
    v = (value or "").strip().lower()
    for a in allowed:
        if a.lower() == v or slug(a) == slug(v):
            return a
    return None


def _int(v) -> int | None:
    try:
        n = int(float(str(v).replace(",", "").replace("₱", "").strip()))
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def parse_csv(data: bytes) -> list[dict]:
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for i, r in enumerate(reader):
        if i >= MAX_ROWS:
            break
        rows.append({(k or "").strip().lower(): (v or "").strip() for k, v in r.items()})
    return rows


XML_MAP = {"referencenumber": "reference", "reference": "reference", "url": "apply_url", "jobtype": "job_type",
           "expirationdate": "expires", "remotetype": "remote", "company": "company_name"}


def parse_xml(data: bytes) -> list[dict]:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False, recover=False)
    try:
        root = etree.fromstring(data, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise HTTPException(422, f"The XML file couldn't be read: {exc.msg}") from exc
    rows = []
    for job in root.iter("job"):
        row = {}
        for child in job:
            if not isinstance(child.tag, str):
                continue
            tag = child.tag.lower()
            row[XML_MAP.get(tag, tag)] = "".join(child.itertext()).strip()
        rows.append(row)
        if len(rows) >= MAX_ROWS:
            break
    return rows


def build_job(row: dict) -> tuple[dict | None, str | None]:
    """One row -> clean job fields, or (None, reason it was skipped)."""
    title, desc = (row.get("title") or "").strip(), (row.get("description") or "").strip()
    if not title:
        return None, "Missing title"
    if len(desc) < 30:
        return None, "Description is missing or shorter than 30 characters"
    given = [x for x in (row.get("city"), row.get("location"), row.get("state"), row.get("country")) if x and x.strip()]
    loc_text = ", ".join(given) if given else "Philippines"     # a row with no location at all is a local job
    remote = (row.get("remote") or "").strip() or (row.get("setup") or "")
    raw = RawJob(external_id=row.get("reference") or "", title=title, url=row.get("apply_url") or "x",
                 location=loc_text, description_html=desc if "<" in desc else None,
                 description_text=None if "<" in desc else desc,
                 workplace_type="remote" if "remote" in remote.lower() else None,
                 employment_type=row.get("job_type") or None, department=row.get("category") or None)
    n = normalize(raw)
    if n is None:
        return None, "Not in the Philippines (or not open to people there)"
    out = {
        "title": n.title, "location": _pick(row.get("location"), LOCATIONS) or n.location,
        "city": (row.get("city") or n.city or None), "setup": _pick(row.get("setup"), SETUPS) or n.setup,
        "job_type": _pick(row.get("job_type"), JOB_TYPES) or n.job_type,
        "level": _pick(row.get("level"), LEVELS) or n.level, "category": _pick(row.get("category"), CATEGORIES) or n.category,
        "shift": _pick(row.get("shift"), SHIFTS) or n.shift, "perks": n.perks,
        "salary_min": _int(row.get("salary_min")) or n.salary_min, "salary_max": _int(row.get("salary_max")) or n.salary_max,
        "skills": [s.strip() for s in (row.get("skills") or "").replace("|", ";").split(";") if s.strip()][:20],
        "apply_url": (row.get("apply_url") or "").strip() or None,
        "description_html": n.description_html if "<" in desc else text_to_html(desc), "description_text": n.description_text,
        "reference": (row.get("reference") or "").strip()[:100] or None, "expires": None,
    }
    if not out["salary_max"] and row.get("salary"):
        from .normalize import parse_salary_text, salary_to_php_month
        sal = parse_salary_text(row["salary"] if any(c in row["salary"] for c in "₱$") or "PHP" in row["salary"].upper()
                                else "PHP " + row["salary"])
        if sal:
            out["salary_min"], out["salary_max"], _ = salary_to_php_month(sal, settings)
    if out["salary_min"] and out["salary_max"] and out["salary_min"] > out["salary_max"]:
        out["salary_min"], out["salary_max"] = out["salary_max"], out["salary_min"]
    if out["apply_url"] and not out["apply_url"].startswith(("https://", "http://")):
        return None, "apply_url must start with https://"
    exp = (row.get("expires") or "").strip()
    if exp:
        try:
            out["expires"] = datetime.fromisoformat(exp[:10]).replace(tzinfo=timezone.utc) + timedelta(hours=23, minutes=59)
        except ValueError:
            return None, f"Expiry date {exp!r} isn't YYYY-MM-DD"
    return out, None


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------

def run_import(db: Session, company: Company, rows: list[dict], *, dry_run: bool, feed: ImportFeed | None = None) -> dict:
    prefix = f"import-{feed.id}-" if feed else "import-0-"
    result = {"rows": len(rows), "valid": 0, "created": 0, "updated": 0, "closed": 0, "errors": [], "dry_run": dry_run}
    seen: set[str] = set()
    now = utcnow()
    for i, row in enumerate(rows, start=2 if not feed or feed.fmt == "csv" else 1):
        job, err = build_job(row)
        if err:
            if len(result["errors"]) < 200:
                result["errors"].append({"row": i, "title": (row.get("title") or "")[:80], "error": err})
            continue
        ref = job["reference"] or hashlib.sha1(f"{job['title']}|{job['location']}|{job['city']}".encode()).hexdigest()[:16]
        ext = prefix + ref
        if ext in seen:
            result["errors"].append({"row": i, "title": job["title"][:80], "error": "Duplicate reference in this file"})
            continue
        seen.add(ext)
        result["valid"] += 1
        if dry_run:
            continue
        existing = db.scalar(select(Job).where(Job.company_id == company.id, Job.external_id == ext))
        j = existing or Job(company_id=company.id, external_id=ext, origin="direct", ph_eligibility="ph_location",
                            posted_at=now, first_seen_at=now, url="", content_hash="", paid_via="import")
        for k in ("title", "location", "city", "setup", "job_type", "level", "category", "shift", "salary_min",
                  "salary_max", "description_html", "description_text", "apply_url"):
            setattr(j, k, job[k])
        j.employment_type = job["job_type"]
        j.skills, j.perks = join_pipes(job["skills"]), join_pipes(job["perks"])
        j.salary_original = f"PHP {job['salary_min']:,}–{job['salary_max']:,} / month" if job["salary_max"] and job["salary_min"] else None
        j.easy_apply = not job["apply_url"]
        j.location_raw = ", ".join(x for x in (j.city, j.location) if x)
        j.expires_at = job["expires"] or (aware(j.expires_at) if existing and j.expires_at else now + timedelta(days=30))
        j.last_seen_at, j.status, j.closed_at = now, "open", None
        j.content_hash = hashlib.sha256(f"{j.title}{j.description_text}".encode()).hexdigest()
        if existing is None:
            db.add(j)
            db.flush()
            result["created"] += 1
        else:
            result["updated"] += 1
        if j.easy_apply:
            j.url = j.apply_url = settings.public_url + job_path(j)
        else:
            j.url = job["apply_url"]
    if feed and not dry_run:      # a feed is the full list: jobs no longer in it are closed
        for j in db.scalars(select(Job).where(Job.company_id == company.id, Job.external_id.like(prefix + "%"),
                                              Job.status == "open")):
            if j.external_id not in seen:
                j.status, j.closed_at = "closed", now
                result["closed"] += 1
    if not dry_run:
        db.commit()
    return result


def _read_upload(data: bytes, filename: str, fmt: str | None) -> list[dict]:
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "Files can be up to 10 MB. Split the file and import it in parts.")
    fmt = fmt or ("xml" if filename.lower().endswith(".xml") or data.lstrip()[:1] == b"<" else "csv")
    rows = parse_xml(data) if fmt == "xml" else parse_csv(data)
    if not rows:
        raise HTTPException(422, "No jobs found in this file. Check that it has a header row (CSV) or <job> elements (XML).")
    return rows


def fetch_feed(url: str) -> bytes:
    if not url.startswith(("https://", "http://")):
        raise HTTPException(422, "Feed URLs must start with https://")
    try:
        with httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": settings.user_agent}) as c:
            with c.stream("GET", url) as r:
                if r.status_code >= 400:
                    raise HTTPException(422, f"The feed returned HTTP {r.status_code}.")
                data = b""
                for chunk in r.iter_bytes():
                    data += chunk
                    if len(data) > MAX_BYTES:
                        raise HTTPException(413, "The feed is larger than 10 MB.")
                return data
    except httpx.HTTPError as exc:
        raise HTTPException(422, f"Couldn't download the feed: {exc}") from exc


def run_feed(db: Session, feed: ImportFeed) -> dict:
    company = db.get(Company, feed.company_id)
    try:
        rows = _read_upload(fetch_feed(feed.url), feed.url, feed.fmt)
        result = run_import(db, company, rows, dry_run=False, feed=feed)
    except HTTPException as exc:
        result = {"error": exc.detail}
    feed.last_run_at, feed.last_result = utcnow(), result
    db.commit()
    return result


def sync_feeds(session_factory) -> int:
    with session_factory() as db:
        due = db.scalars(select(ImportFeed).where(ImportFeed.sync_daily.is_(True))).all()
        n = 0
        for f in due:
            if f.last_run_at is None or aware(f.last_run_at) < utcnow() - timedelta(hours=23):
                run_feed(db, f)
                n += 1
        return n


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/api/import/template.csv", response_class=PlainTextResponse)
def template():
    return PlainTextResponse(TEMPLATE, media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="sinag-jobs-import-template.csv"'})


def _admin_company(db: Session, company_id: int | None, new_name: str | None) -> Company:
    if company_id:
        c = db.get(Company, company_id)
        if c is None:
            raise HTTPException(404, "Company not found.")
        if c.source != "direct":
            raise HTTPException(400, "Pick an employer company. Crawled companies are filled by the crawler.")
        return c
    name = (new_name or "").strip()
    if len(name) < 2:
        raise HTTPException(422, "Pick a company or type a new company name.")
    base, n = slugify(name) or "company", 2
    s = base
    while db.scalar(select(Company.id).where(Company.slug == s)):
        s, n = f"{base}-{n}", n + 1
    c = Company(slug=s, name=name, source="direct", source_key="", approval="approved", verified=True, active=True, options={})
    db.add(c)
    db.flush()
    return c


@router.post("/api/admin/import", dependencies=[Depends(require_admin_user)])
async def admin_import(file: UploadFile = File(...), company_id: int | None = Form(None), new_company: str | None = Form(None),
                       dry_run: bool = Form(True), db: Session = Depends(get_session)):
    data = await file.read(MAX_BYTES + 1)
    rows = _read_upload(data, file.filename or "", None)
    company = _admin_company(db, company_id, new_company)
    result = run_import(db, company, rows, dry_run=dry_run)
    if dry_run:
        db.rollback()
    return {**result, "company": {"id": company.id, "name": company.name} if not dry_run or company_id else {"name": company.name}}


@router.get("/api/admin/import/companies", dependencies=[Depends(require_admin_user)])
def importable_companies(db: Session = Depends(get_session)):
    return [{"id": c.id, "name": c.name, "approval": c.approval}
            for c in db.scalars(select(Company).where(Company.source == "direct").order_by(Company.name))]


class FeedIn(BaseModel):
    company_id: int | None = None
    new_company: str | None = None
    url: str = Field(max_length=1000)
    fmt: str = Field("xml", pattern="^(csv|xml)$")
    sync_daily: bool = True


@router.get("/api/admin/import/feeds", dependencies=[Depends(require_admin_user)])
def list_feeds(db: Session = Depends(get_session)):
    out = []
    for f in db.scalars(select(ImportFeed).order_by(ImportFeed.id.desc())):
        c = db.get(Company, f.company_id)
        out.append({"id": f.id, "company": c.name if c else "?", "url": f.url, "fmt": f.fmt, "sync_daily": f.sync_daily,
                    "last_run_at": aware(f.last_run_at), "last_result": f.last_result})
    return out


@router.post("/api/admin/import/feeds", status_code=201, dependencies=[Depends(require_admin_user)])
def add_feed(body: FeedIn, db: Session = Depends(get_session)):
    company = _admin_company(db, body.company_id, body.new_company)
    f = ImportFeed(company_id=company.id, url=body.url.strip(), fmt=body.fmt, sync_daily=body.sync_daily)
    db.add(f)
    db.commit()
    return {"id": f.id, "result": run_feed(db, f)}


@router.post("/api/admin/import/feeds/{feed_id}/run", dependencies=[Depends(require_admin_user)])
def rerun_feed(feed_id: int, db: Session = Depends(get_session)):
    f = db.get(ImportFeed, feed_id)
    if f is None:
        raise HTTPException(404, "Feed not found.")
    return run_feed(db, f)


@router.delete("/api/admin/import/feeds/{feed_id}", dependencies=[Depends(require_admin_user)])
def delete_feed(feed_id: int, close_jobs: bool = False, db: Session = Depends(get_session)):
    f = db.get(ImportFeed, feed_id)
    if f is None:
        raise HTTPException(404, "Feed not found.")
    if close_jobs:
        for j in db.scalars(select(Job).where(Job.external_id.like(f"import-{f.id}-%"), Job.status == "open")):
            j.status, j.closed_at = "closed", utcnow()
    db.delete(f)
    db.commit()
    return {"ok": True}


@router.post("/api/employer/import")
async def employer_import(file: UploadFile = File(...), dry_run: bool = Form(True), user=Depends(require_employer),
                          company: Company = Depends(need_company), db: Session = Depends(get_session)):
    from .auth import require_verified
    from .billing import require_feature
    require_verified(user)
    require_feature(db, company, "bulk_import", "Bulk import")
    data = await file.read(MAX_BYTES + 1)
    rows = _read_upload(data, file.filename or "", None)
    return run_import(db, company, rows, dry_run=dry_run)
