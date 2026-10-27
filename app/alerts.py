"""Job alert emails: a daily digest (8 AM Manila by default) and instant alerts after each crawl."""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload, sessionmaker

from .common import public_job_filter
from .config import Settings, settings as default_settings
from .mailer import queue_email
from .matching import match_score
from .models import AlertSubscription, AppState, CandidateProfile, Company, Job, aware, utcnow

log = logging.getLogger("sinag.alerts")
MAX_JOBS_PER_EMAIL = 10


def _matching_jobs(db: Session, a: AlertSubscription, since: datetime, cfg: Settings) -> list[tuple[Job, int | None]]:
    q = (select(Job).join(Company).options(joinedload(Job.company))
         .where(public_job_filter(), Job.posted_at > since).order_by(Job.posted_at.desc()).limit(300))
    if a.category:
        q = q.where(Job.category == a.category)
    if a.location:
        q = q.where(Job.location == a.location)
    if a.min_pay:
        q = q.where(Job.salary_max >= a.min_pay)
    if a.keywords:
        for w in a.keywords.split()[:5]:
            q = q.where(or_(Job.title.ilike(f"%{w}%"), Company.name.ilike(f"%{w}%"), Job.description_text.ilike(f"%{w}%")))
    jobs = db.scalars(q).all()
    profile = db.get(CandidateProfile, a.user_id) if (a.use_profile and a.user_id) else None
    out: list[tuple[Job, int | None]] = []
    for j in jobs:
        if profile is not None:
            s = match_score(profile, j).score
            if s < cfg.match_threshold:
                continue
            out.append((j, s))
        else:
            out.append((j, None))
    if profile is not None:
        out.sort(key=lambda x: -(x[1] or 0))
    return out


def _email_body(a: AlertSubscription, jobs: list[tuple[Job, int | None]], cfg: Settings) -> str:
    lines = [f"{len(jobs)} new job{'s' if len(jobs) != 1 else ''} matching your alert"
             + (f" ({', '.join(x for x in (a.category, a.location, a.keywords) if x)})" if (a.category or a.location or a.keywords) else "")
             + ":", ""]
    for j, s in jobs[:MAX_JOBS_PER_EMAIL]:
        pay = f"₱{j.salary_min:,}–₱{j.salary_max:,}/mo" if j.salary_max else "Salary not stated"
        lines.append(f"• {j.title} at {j.company.name}")
        lines.append(f"  {j.location} · {j.setup} · {pay}" + (f" · {s}% match" if s is not None else ""))
        lines.append(f"  {cfg.public_url}/#job-{j.id}")
        lines.append("")
    if len(jobs) > MAX_JOBS_PER_EMAIL:
        lines.append(f"See all {len(jobs)}: {cfg.public_url}/")
    lines += ["", f"Stop these emails: {cfg.public_url}/api/alerts/unsubscribe?token={a.unsubscribe_token}"]
    return "\n".join(lines)


def send_alerts(session_factory: sessionmaker, frequency: str, cfg: Settings = default_settings,
                now: datetime | None = None) -> int:
    """Queue one email per active subscription of this frequency that has new matching jobs."""
    now = now or utcnow()
    queued = 0
    with session_factory() as db:
        subs = db.scalars(select(AlertSubscription).where(
            AlertSubscription.active.is_(True), AlertSubscription.confirmed.is_(True),
            AlertSubscription.frequency == frequency)).all()
        for a in subs:
            since = aware(a.last_sent_at) or (now - timedelta(days=1))
            jobs = _matching_jobs(db, a, since, cfg)
            a.unsubscribe_token = a.unsubscribe_token or secrets.token_urlsafe(24)
            a.last_sent_at = now
            if not jobs:
                continue
            subject = (f"{len(jobs)} new job{'s' if len(jobs) != 1 else ''} for you on Sinag Jobs"
                       if frequency == "daily" else f"New job: {jobs[0][0].title} at {jobs[0][0].company.name}")
            queue_email(db, a.email, subject, _email_body(a, jobs, cfg))
            queued += 1
        db.commit()
    if queued:
        log.info("%s alerts: %d emails queued", frequency, queued)
    return queued


def daily_digest_due(session_factory: sessionmaker, cfg: Settings = default_settings,
                     now: datetime | None = None) -> bool:
    """True once per local day, at or after DIGEST_HOUR. Marks the day as done."""
    now = now or utcnow()
    local = now.astimezone(ZoneInfo(cfg.timezone))
    if local.hour < cfg.digest_hour:
        return False
    today = local.date().isoformat()
    with session_factory() as db:
        row = db.get(AppState, "daily_digest_date")
        if row is not None and row.value == today:
            return False
        if row is None:
            db.add(AppState(key="daily_digest_date", value=today))
        else:
            row.value = today
        db.commit()
    return True
