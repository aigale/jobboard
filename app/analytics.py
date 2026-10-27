"""Analytics and reporting.

Privacy-friendly by design: no cookies and no tracking IDs. Daily unique visitors are counted with a
hash of (secret daily salt + IP + browser) that changes every day; the hashes are deleted after two days,
and only the daily totals are kept. Bots are ignored.

Recorded counters (per day, Manila time):
  visit, unique, job_view (key = job id), apply_click (external jobs, key = job id),
  application (Easy Apply, key = job id), search (key = search words), signup (key = seeker | employer)
"""
from __future__ import annotations

import hashlib
import secrets
import statistics
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import _client, rate_limit, require_admin_user
from .config import settings
from .db import get_session
from .employer import need_company
from .models import (AppState, Application, ApplicationEvent, CandidateInvite, Company, CompanyMember,
                     DailyStat, DailyVisitor, Job, Order, User, aware, utcnow)

router = APIRouter(tags=["analytics"])
BOT_WORDS = ("bot", "crawl", "spider", "slurp", "preview", "headless", "lighthouse")
METRICS = ("visit", "unique", "job_view", "apply_click", "application", "signup")


def today(now: datetime | None = None) -> str:
    return (now or utcnow()).astimezone(ZoneInfo(settings.timezone)).date().isoformat()


def record(db: Session, metric: str, key: str = "", n: int = 1, day: str | None = None) -> None:
    """Add to a daily counter in its own small transaction, so it never disturbs the caller's work."""
    day = day or today()
    key = (key or "")[:100]
    with Session(bind=db.get_bind()) as s:
        for _ in range(3):
            done = s.execute(update(DailyStat).where(DailyStat.day == day, DailyStat.metric == metric, DailyStat.key == key)
                             .values(count=DailyStat.count + n)).rowcount
            if not done:
                s.add(DailyStat(day=day, metric=metric, key=key, count=n))
            try:
                s.commit()
                return
            except IntegrityError:          # someone else inserted the same row first; add to it instead
                s.rollback()


def _salt(db: Session) -> str:
    row = db.get(AppState, "analytics_secret")
    if row is None:
        row = AppState(key="analytics_secret", value=secrets.token_hex(16))
        db.add(row)
        db.commit()
    return row.value


def is_bot(request: Request) -> bool:
    ua = (request.headers.get("user-agent") or "").lower()
    return not ua or any(w in ua for w in BOT_WORDS)


class TrackIn(BaseModel):
    type: str = Field(pattern="^(visit|apply_click|search)$")
    job_id: int | None = None
    q: str | None = Field(None, max_length=100)


@router.post("/api/track", status_code=204)
def track(body: TrackIn, request: Request, db: Session = Depends(get_session)):
    if is_bot(request):
        return
    ip = _client(request)
    try:
        rate_limit(f"track:{ip}", limit=120, window=60)
    except HTTPException:
        return
    day = today()
    if body.type == "visit":
        record(db, "visit", day=day)
        h = hashlib.sha256(f"{_salt(db)}:{day}:{ip}:{request.headers.get('user-agent', '')}".encode()).hexdigest()[:32]
        with Session(bind=db.get_bind()) as s:
            s.add(DailyVisitor(day=day, hash=h))
            try:
                s.commit()
                record(db, "unique", day=day)
            except IntegrityError:
                s.rollback()
    elif body.type == "apply_click" and body.job_id:
        record(db, "apply_click", str(body.job_id), day=day)
    elif body.type == "search" and body.q:
        q = " ".join(body.q.lower().split())[:60]
        if len(q) >= 2:
            record(db, "search", q, day=day)


def cleanup(db: Session) -> None:
    cutoff = (date.fromisoformat(today()) - timedelta(days=2)).isoformat()
    db.execute(delete(DailyVisitor).where(DailyVisitor.day < cutoff))
    db.commit()


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def _days(n: int) -> list[str]:
    end = date.fromisoformat(today())
    return [(end - timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]


def _series(db: Session, days: list[str], metric: str, keys: set[str] | None = None) -> list[int]:
    q = select(DailyStat.day, func.sum(DailyStat.count)).where(DailyStat.metric == metric, DailyStat.day >= days[0])
    if keys is not None:
        q = q.where(DailyStat.key.in_(keys or {"-"}))
    by_day = dict(db.execute(q.group_by(DailyStat.day)).all())
    return [int(by_day.get(d, 0)) for d in days]


def _per_key(db: Session, days: list[str], metric: str, keys: set[str] | None = None) -> dict[str, int]:
    q = select(DailyStat.key, func.sum(DailyStat.count)).where(DailyStat.metric == metric, DailyStat.day >= days[0])
    if keys is not None:
        q = q.where(DailyStat.key.in_(keys or {"-"}))
    return {k: int(v) for k, v in db.execute(q.group_by(DailyStat.key)).all()}


def _pct(a: int, b: int) -> float | None:
    return round(100 * a / b, 1) if b else None


def _hours_to_first_view(db: Session, job_ids: list[int], since: datetime) -> float | None:
    rows = db.execute(select(Application.created_at, Application.viewed_at).where(
        Application.job_id.in_(job_ids or [-1]), Application.created_at >= since, Application.viewed_at.is_not(None))).all()
    hours = [(aware(v) - aware(c)).total_seconds() / 3600 for c, v in rows if v and c]
    return round(statistics.median(hours), 1) if hours else None


@router.get("/api/admin/analytics", dependencies=[Depends(require_admin_user)])
def site_report(days: int = Query(30, ge=7, le=365), db: Session = Depends(get_session)):
    ds = _days(days)
    since = datetime.fromisoformat(ds[0]).replace(tzinfo=ZoneInfo(settings.timezone))
    series = {m: _series(db, ds, m) for m in METRICS}
    signups = _per_key(db, ds, "signup")
    totals = {m: sum(v) for m, v in series.items()}
    views = _per_key(db, ds, "job_view")
    clicks = _per_key(db, ds, "apply_click")
    apps = _per_key(db, ds, "application")
    top_ids = sorted(views, key=lambda k: -views[k])[:15]
    jobs = {str(j.id): j for j in db.scalars(select(Job).where(Job.id.in_([int(k) for k in top_ids if k.isdigit()] or [-1])))}
    companies = {c.id: c.name for c in db.scalars(select(Company).where(Company.id.in_({j.company_id for j in jobs.values()} or {-1})))}
    top_jobs = [{"id": int(k), "title": jobs[k].title, "company": companies.get(jobs[k].company_id, ""),
                 "easy_apply": jobs[k].origin == "direct" and jobs[k].easy_apply, "views": views[k],
                 "applies": clicks.get(k, 0) + apps.get(k, 0), "conversion": _pct(clicks.get(k, 0) + apps.get(k, 0), views[k])}
                for k in top_ids if k in jobs]
    searches = _per_key(db, ds, "search")
    top_searches = [{"q": k, "count": v} for k, v in sorted(searches.items(), key=lambda kv: -kv[1])[:15]]

    # Recruiter engagement: employers who post on Sinag
    engagement = []
    for c in db.scalars(select(Company).where(Company.source == "direct", Company.approval == "approved")):
        job_ids = list(db.scalars(select(Job.id).where(Job.company_id == c.id, Job.origin == "direct")))
        received = db.scalar(select(func.count()).select_from(Application).where(
            Application.job_id.in_(job_ids or [-1]), Application.created_at >= since)) or 0
        viewed = db.scalar(select(func.count()).select_from(Application).where(
            Application.job_id.in_(job_ids or [-1]), Application.created_at >= since, Application.viewed_at.is_not(None))) or 0
        moves = db.scalar(select(func.count()).select_from(ApplicationEvent).join(Application).where(
            Application.job_id.in_(job_ids or [-1]), ApplicationEvent.kind == "stage", ApplicationEvent.at >= since)) or 0
        last_login = db.scalar(select(func.max(User.last_login_at)).join(CompanyMember, CompanyMember.user_id == User.id)
                               .where(CompanyMember.company_id == c.id))
        engagement.append({
            "company": c.name, "members": db.scalar(select(func.count()).select_from(CompanyMember).where(CompanyMember.company_id == c.id)),
            "jobs_posted": db.scalar(select(func.count()).select_from(Job).where(Job.company_id == c.id, Job.origin == "direct",
                                                                                  Job.first_seen_at >= since)) or 0,
            "applications": received, "viewed_pct": _pct(viewed, received), "stage_moves": moves,
            "hours_to_first_view": _hours_to_first_view(db, job_ids, since),
            "invites": db.scalar(select(func.count()).select_from(CandidateInvite).where(
                CandidateInvite.company_id == c.id, CandidateInvite.created_at >= since)) or 0,
            "last_login": aware(last_login)})
    engagement.sort(key=lambda e: (-e["applications"], e["company"]))

    revenue = db.execute(select(Order.product_name, func.count(), func.sum(Order.amount_php)).where(
        Order.status == "paid", Order.paid_at >= since).group_by(Order.product_name)).all()
    return {
        "days": ds, "series": series, "totals": totals,
        "signups": {"seeker": signups.get("seeker", 0), "employer": signups.get("employer", 0)},
        "conversion": {"view_to_apply": _pct(totals["apply_click"] + totals["application"], totals["job_view"]),
                       "visitor_to_signup": _pct(totals["signup"], totals["unique"])},
        "top_jobs": top_jobs, "top_searches": top_searches, "recruiters": engagement,
        "revenue_test": [{"product": n, "orders": c, "amount_php": int(a or 0)} for n, c, a in revenue],
    }


@router.get("/api/employer/analytics")
def company_report(days: int = Query(30, ge=7, le=365), company: Company = Depends(need_company),
                   db: Session = Depends(get_session)):
    ds = _days(days)
    since = datetime.fromisoformat(ds[0]).replace(tzinfo=ZoneInfo(settings.timezone))
    jobs = db.scalars(select(Job).where(Job.company_id == company.id, Job.origin == "direct", Job.status != "draft")
                      .order_by(Job.id.desc())).all()
    keys = {str(j.id) for j in jobs}
    views, apps = _per_key(db, ds, "job_view", keys), _per_key(db, ds, "application", keys)
    clicks = _per_key(db, ds, "apply_click", keys)
    stages = dict(db.execute(select(Application.stage, func.count()).where(
        Application.job_id.in_([j.id for j in jobs] or [-1])).group_by(Application.stage)).all())
    per_job = []
    for j in jobs:
        k = str(j.id)
        total_apps = apps.get(k, 0) + clicks.get(k, 0)
        avg_score = db.scalar(select(func.avg(Application.score)).where(Application.job_id == j.id))
        per_job.append({"id": j.id, "title": j.title, "status": j.status, "views": views.get(k, 0), "applications": total_apps,
                        "conversion": _pct(total_apps, views.get(k, 0)),
                        "avg_score": round(avg_score) if avg_score is not None else None})
    return {"days": ds,
            "series": {"job_view": _series(db, ds, "job_view", keys),
                       "application": [a + b for a, b in zip(_series(db, ds, "application", keys), _series(db, ds, "apply_click", keys))]},
            "totals": {"views": sum(views.values()), "applications": sum(apps.values()) + sum(clicks.values())},
            "hours_to_first_view": _hours_to_first_view(db, [j.id for j in jobs], since),
            "stages": stages, "jobs": per_job}
