"""HTTP API for the job board, plus the website.

Run with:  uvicorn app.api:app --reload
"""
from __future__ import annotations

import asyncio
import html
import logging
import secrets
from collections import Counter
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, joinedload

from . import admin, analytics, auth, billing, company, employer, importer, interviews, messages, oauth, screening, seeker, seo, talent
from .auth import optional_user
from .common import candidate_status, is_featured, job_path, public_job_filter
from .config import ROOT, settings
from .db import get_session, init_db
from .matching import match_score
from .models import AlertSubscription, Application, CandidateProfile, Company, CrawlRun, Job, User, aware, utcnow
from .schemas import AlertIn, CompanyOut, FacetCount, Facets, JobPage, JobSummary
from .sources import FEED_SOURCES

log = logging.getLogger("sinag.api")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="Sinag Jobs API", version="2.0", lifespan=lifespan,
              description="Fresh, verified jobs in the Philippines, with accounts, Easy Apply and an applicant pipeline.")
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["GET", "POST"],
                   allow_headers=["*"])


@app.middleware("http")
async def same_origin_writes(request: Request, call_next):
    """Refuse cross-site form posts and scripts that change data (defence on top of SameSite cookies)."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.url.path.startswith("/api/"):
        origin = request.headers.get("origin")
        if origin:
            host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
            allowed = {o.rstrip("/") for o in settings.cors_origins if o != "*"}
            if urlsplit(origin).netloc != host and origin.rstrip("/") not in allowed:
                return JSONResponse({"detail": "Requests from other websites aren't allowed."}, status_code=403)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    # HTML must be revalidated so a deploy cannot leave an old page pointing at an
    # incompatible cached script. Fingerprinted/versioned assets are handled in the page.
    if request.method == "GET" and (request.url.path == "/" or request.url.path.endswith(".html")):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


for r in (auth.router, seeker.router, seeker.apply_router, employer.router, admin.router, screening.employer_router,
          screening.seeker_router, talent.router, talent.seeker_router, company.router, company.public_router,
          billing.router, billing.admin_router, analytics.router, importer.router, seo.router,
          messages.router, interviews.router, interviews.employer_router, oauth.router, oauth.me_router):
    app.include_router(r)


def summary_dict(j: Job) -> dict:
    return dict(
        id=j.id, title=j.title,
        company=CompanyOut(name=j.company.name, slug=j.company.slug, logo_url=j.company.logo_url,
                           verified=j.company.verified).model_dump(),
        location=j.location, city=j.city, setup=j.setup, shift=j.shift, level=j.level, category=j.category,
        perks=j.perk_list, salary_min=j.salary_min, salary_max=j.salary_max, salary_original=j.salary_original,
        posted_at=aware(j.posted_at), featured=is_featured(j), url=j.url, job_type=j.job_type,
        industry=j.company.industry, easy_apply=bool(j.easy_apply and j.origin == "direct"),
    )


@app.get("/api/health")
def health(db: Session = Depends(get_session)):
    db.execute(select(1))
    return {"ok": True}


@app.get("/api/jobs", response_model=JobPage)
def list_jobs(
    q: str | None = Query(None, max_length=100, description="Words in the title, company, category or skills"),
    location: str | None = None,
    category: str | None = None,
    industry: list[str] = Query(default=[]),
    job_type: list[str] = Query(default=[]),
    setup: list[str] = Query(default=[]),
    shift: list[str] = Query(default=[]),
    level: list[str] = Query(default=[]),
    perk: list[str] = Query(default=[], description="Every listed perk must be offered"),
    min_pay: int = Query(0, ge=0, description="Minimum monthly pay in PHP; hides jobs without a salary"),
    max_pay: int = Query(0, ge=0, description="Maximum monthly pay in PHP (0 = no limit)"),
    easy_apply: bool = False,
    company: str | None = Query(None, description="Company slug"),
    ids: str | None = Query(None, description="Comma-separated job ids, for saved jobs"),
    sort: str = Query("new", pattern="^(new|pay)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_session),
):
    stmt = select(Job).join(Company).where(public_job_filter())
    if q:
        for word in q.split()[:6]:
            like = f"%{word}%"
            stmt = stmt.where(or_(Job.title.ilike(like), Company.name.ilike(like), Job.category.ilike(like),
                                  Job.department.ilike(like), Job.skills.ilike(like)))
    if location:
        stmt = stmt.where(Job.location == location)
    if category:
        stmt = stmt.where(Job.category == category)
    if industry:
        stmt = stmt.where(Company.industry.in_(industry))
    if job_type:
        stmt = stmt.where(Job.job_type.in_(job_type))
    if setup:
        stmt = stmt.where(Job.setup.in_(setup))
    if shift:
        stmt = stmt.where(Job.shift.in_(shift))
    if level:
        stmt = stmt.where(Job.level.in_(level))
    for p in perk:
        stmt = stmt.where(Job.perks.like(f"%|{p}|%"))
    if min_pay:
        stmt = stmt.where(Job.salary_max >= min_pay)
    if max_pay:
        stmt = stmt.where(Job.salary_min <= max_pay)
    if easy_apply:
        stmt = stmt.where(Job.easy_apply.is_(True), Job.origin == "direct")
    if company:
        stmt = stmt.where(Company.slug == company)
    if ids:
        id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()][:200]
        stmt = stmt.where(Job.id.in_(id_list or [-1]))

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    from sqlalchemy import case
    featured_now = case(((Job.featured.is_(True)) | (Job.featured_until > utcnow()), 1), else_=0)
    order = ([featured_now.desc(), Job.salary_max.desc().nulls_last(), Job.posted_at.desc()] if sort == "pay"
             else [featured_now.desc(), Job.posted_at.desc(), Job.id.desc()])
    rows = db.scalars(stmt.options(joinedload(Job.company)).order_by(*order)
                      .offset((page - 1) * page_size).limit(page_size)).all()
    return JobPage(total=total, page=page, page_size=page_size, jobs=[JobSummary(**summary_dict(j)) for j in rows])


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int, request: Request, db: Session = Depends(get_session), user: User | None = Depends(optional_user)):
    j = db.scalar(select(Job).options(joinedload(Job.company), joinedload(Job.questions))
                  .join(Company).where(Job.id == job_id, public_job_filter()))
    if j is None:
        raise HTTPException(404, "This job doesn't exist or is no longer open.")
    if not analytics.is_bot(request):
        db.execute(update(Job).where(Job.id == j.id).values(view_count=Job.view_count + 1))
        db.commit()
        analytics.record(db, "job_view", str(j.id))
    direct = j.origin == "direct"
    out = {
        **summary_dict(j), "apply_url": (j.apply_url or j.url) if not (direct and j.easy_apply) else None,
        "page_url": job_path(j),
        "location_raw": j.location_raw, "department": j.department, "employment_type": j.employment_type,
        "description_html": j.description_html, "status": j.status, "first_seen_at": aware(j.first_seen_at),
        "skills": j.skill_list,
        "company_about": j.company.about if direct else None, "company_website": j.company.website if direct else None,
        "company_tagline": j.company.tagline if direct else None,
        "has_company_page": bool(direct and (j.company.about or j.company.tagline or j.company.culture_values)),
        "has_test": bool(direct and j.assessment_id), "has_chat": bool(direct and j.chat_screening and j.chat_topics),
        "questions": [{"id": q.id, "text": q.text, "kind": q.kind, "options": q.options, "required": q.required}
                      for q in j.questions] if direct else [],
        "my_application": None, "match": None,
    }
    if user is not None and user.role == "seeker":
        a = db.scalar(select(Application).where(Application.job_id == j.id, Application.user_id == user.id))
        if a is not None:
            out["my_application"] = {"id": a.id, "status": candidate_status(a), "stage": a.stage}
        p = db.get(CandidateProfile, user.id)
        if p is not None and p.skill_list:
            out["match"] = match_score(p, j).as_dict()
    return out


@app.get("/api/facets", response_model=Facets)
def facets(db: Session = Depends(get_session)):
    base = (select(Job.id, Job.company_id, Job.location, Job.category, Job.setup, Job.shift, Job.level, Job.perks,
                   Job.job_type, Job.easy_apply, Job.origin, Company.industry)
            .join(Company).where(public_job_filter()).subquery())

    def counts(col: str) -> list[FacetCount]:
        c = getattr(base.c, col)
        rows = db.execute(select(c, func.count()).group_by(c).order_by(func.count().desc())).all()
        return [FacetCount(value=v, count=n) for v, n in rows if v and v != "Not stated" or (v and col == "shift")]

    perk_counter: Counter[str] = Counter()
    for (perks,) in db.execute(select(base.c.perks)):
        perk_counter.update(p for p in (perks or "").split("|") if p)

    return Facets(
        total_open=db.scalar(select(func.count()).select_from(base)) or 0,
        companies=db.scalar(select(func.count(func.distinct(base.c.company_id)))) or 0,
        last_crawl_at=aware(db.scalar(select(func.max(CrawlRun.finished_at)))),
        location=counts("location"), category=counts("category"), setup=counts("setup"),
        shift=counts("shift"), level=counts("level"), job_type=counts("job_type"), industry=counts("industry"),
        perk=[FacetCount(value=k, count=v) for k, v in perk_counter.most_common()],
        easy_apply=db.scalar(select(func.count()).select_from(base).where(
            base.c.easy_apply.is_(True), base.c.origin == "direct")) or 0,
    )


@app.get("/api/companies")
def companies(db: Session = Depends(get_session)):
    rows = db.execute(
        select(Company, func.count(Job.id))
        .outerjoin(Job, (Job.company_id == Company.id) & (Job.status == "open"))
        .where(Company.active.is_(True), Company.approval == "approved",
               Company.source.notin_(tuple(FEED_SOURCES)))
        .group_by(Company.id).order_by(Company.name)).all()
    return [{"name": c.name, "slug": c.slug, "logo_url": c.logo_url, "careers_url": c.careers_url,
             "industry": c.industry, "open_jobs": n, "verified": c.verified} for c, n in rows]


@app.post("/api/alerts", status_code=201)
def create_alert(body: AlertIn, request: Request, background: BackgroundTasks, db: Session = Depends(get_session)):
    """Alert sign-up without an account (the form at the bottom of the board). Confirmed by email first."""
    from .auth import _client, make_email_token, rate_limit
    from .mailer import flush_soon, queue_email
    rate_limit(f"alert:{_client(request)}", limit=10, window=3600)
    a = db.scalar(select(AlertSubscription).where(
        AlertSubscription.email == body.email,
        AlertSubscription.category.is_(None) if body.category is None else AlertSubscription.category == body.category,
        AlertSubscription.location.is_(None) if body.location is None else AlertSubscription.location == body.location))
    if a is not None and a.active and a.confirmed:
        return {"ok": True, "message": "You're already subscribed to this alert."}
    if a is None:
        a = AlertSubscription(email=body.email, category=body.category, location=body.location,
                              unsubscribe_token=secrets.token_urlsafe(24), frequency="daily")
        db.add(a)
    a.active, a.confirmed = True, False
    db.flush()
    token = make_email_token(db, "alert", data={"alert_id": a.id}, hours=72)
    what = ", ".join(x for x in (body.category, body.location) if x) or "new jobs"
    queue_email(db, body.email, "Confirm your Sinag Jobs alert",
                f"Confirm that you want a daily email with {what}:\n\n{settings.public_url}/api/alerts/confirm?token={token}\n\n"
                "If you didn't ask for this, ignore this email and you won't hear from us.\n")
    db.commit()
    flush_soon(background)
    return {"ok": True, "message": f"Almost done: we sent a confirmation link to {body.email}."}


@app.get("/api/alerts/confirm", response_class=HTMLResponse)
def confirm_alert(token: str, db: Session = Depends(get_session)):
    from .auth import use_email_token
    t = use_email_token(db, token, "alert")
    a = db.get(AlertSubscription, t.data.get("alert_id")) if t else None
    if a is not None:
        a.confirmed, a.active, a.last_sent_at = True, True, a.last_sent_at or utcnow()
        db.commit()
    return _simple_page("Alert confirmed" if a else "Link expired",
                        "You'll get new matching jobs by email every morning at 8 AM." if a
                        else "This confirmation link has expired or was already used. Sign up again on the job board.")


def _simple_page(title: str, msg: str) -> str:
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)} · Sinag Jobs</title>"
            f"<meta name='theme-color' content='#0038A8'><link rel='stylesheet' href='/site.css'>"
            f"<script src='/theme.js'></script></head>"
            f"<body><main class='wrap page'><h1>{html.escape(title)}</h1><p>{html.escape(msg)}</p>"
            f"<p><a href='/'>Back to Sinag Jobs</a></p></main></body></html>")


@app.get("/api/alerts/unsubscribe", response_class=HTMLResponse)
def unsubscribe(token: str, db: Session = Depends(get_session)):
    a = db.scalar(select(AlertSubscription).where(AlertSubscription.unsubscribe_token == token))
    if a is not None:
        a.active = False
        db.commit()
    return _simple_page("Unsubscribed", "You won't get this job alert anymore." if a is not None
                        else "This link has already been used or isn't valid.")


@app.get("/api/push/key")
def push_key():
    from . import push
    return {"enabled": push.enabled(), "public_key": settings.vapid_public_key or None}


# ---------------------------------------------------------------------------
# Operator endpoints: ADMIN_TOKEN header or a signed-in admin
# ---------------------------------------------------------------------------

def _require_operator(authorization: str = Header(""), user: User | None = Depends(optional_user)) -> None:
    if user is not None and user.role == "admin":
        return
    if settings.admin_token and authorization == f"Bearer {settings.admin_token}":
        return
    raise HTTPException(401, "Sign in as an admin, or send 'Authorization: Bearer <ADMIN_TOKEN>'.")


@app.get("/api/admin/status", dependencies=[Depends(_require_operator)])
def admin_status(db: Session = Depends(get_session)):
    runs = db.scalars(select(CrawlRun).order_by(CrawlRun.id.desc()).limit(10)).all()
    failing = db.scalars(select(Company).where(Company.last_status == "error")).all()
    return {
        "recent_runs": [{"id": r.id, "started_at": aware(r.started_at), "finished_at": aware(r.finished_at),
                         "companies_ok": r.companies_ok, "companies_failed": r.companies_failed,
                         "new": r.jobs_new, "updated": r.jobs_updated, "closed": r.jobs_closed,
                         "skipped_not_ph": r.jobs_skipped_not_ph} for r in runs],
        "failing_companies": [{"slug": c.slug, "source": c.source, "error": c.last_error,
                               "failures_in_a_row": c.consecutive_failures} for c in failing],
    }


_crawl_lock = asyncio.Lock()


@app.post("/api/admin/crawl", status_code=202, dependencies=[Depends(_require_operator)])
async def admin_crawl(background: BackgroundTasks, company: str | None = None):
    if _crawl_lock.locked():
        return {"started": False, "message": "A crawl is already running."}

    async def run() -> None:
        from .crawler import crawl, sync_companies
        from .db import SessionLocal
        async with _crawl_lock:
            with SessionLocal() as s:
                sync_companies(s, settings.companies_file)
            await crawl(only_slug=company)

    background.add_task(run)
    return {"started": True, "message": "Crawl started. Check /api/admin/status for results."}


# The website itself
app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="site")
