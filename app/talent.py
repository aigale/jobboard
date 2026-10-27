"""Resume database: verified employers search job seekers who chose to be found, and invite them to apply.

Privacy rules:
- Only profiles with "Let verified employers find me" switched on appear, and only to approved companies.
- Email and phone stay hidden. Employers reach candidates through invites; contact details are
  shared only when the candidate applies.
- Employers can open the candidate's latest resume only (not certificates or other files).
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import case, exists, func, or_, select
from sqlalchemy.orm import Session, joinedload

from .auth import require_employer, require_seeker, require_verified
from .common import is_public
from .config import settings
from .db import get_session
from .employer import need_company
from .mailer import flush_soon, queue_email
from .models import (Application, CandidateInvite, CandidateProfile, Company, Document, Job, Notification, User,
                     aware, utcnow)
from .seeker import file_response

router = APIRouter(prefix="/api/employer/candidates", tags=["resume database"])
seeker_router = APIRouter(prefix="/api/me/invites", tags=["job seekers"])
INVITES_PER_DAY = 50


def need_verified_company(company: Company = Depends(need_company), db: Session = Depends(get_session)) -> Company:
    if company.approval != "approved":
        raise HTTPException(403, "The resume database opens once your company is verified.")
    from .billing import require_feature
    require_feature(db, company, "resume_db", "The resume database")
    return company


def _latest_resume(db: Session, user_id: int) -> Document | None:
    return db.scalar(select(Document).where(Document.user_id == user_id, Document.kind == "resume",
                                            Document.archived.is_(False)).order_by(Document.id.desc()).limit(1))


def _visible(db: Session, user_id: int) -> tuple[User, CandidateProfile]:
    row = db.execute(select(User, CandidateProfile).join(CandidateProfile, CandidateProfile.user_id == User.id)
                     .where(User.id == user_id, User.is_active.is_(True), User.role == "seeker",
                            CandidateProfile.searchable.is_(True))).first()
    if row is None:
        raise HTTPException(404, "This candidate isn't in the resume database anymore.")
    return row


def candidate_card(p: CandidateProfile, has_resume: bool) -> dict:
    return {"id": p.user_id, "name": p.full_name, "headline": p.headline, "location": p.location,
            "skills": p.skill_list, "years_experience": p.years_experience, "desired_salary": p.desired_salary,
            "summary": (p.summary or "")[:280], "has_resume": has_resume, "updated_at": aware(p.updated_at),
            "highlighted": bool(p.highlighted_until and aware(p.highlighted_until) > utcnow())}


@router.get("")
def search(q: str | None = Query(None, max_length=100), skills: list[str] = Query(default=[]),
           location: str | None = None, min_years: int | None = Query(None, ge=0, le=60),
           max_salary: int | None = Query(None, ge=0), page: int = Query(1, ge=1),
           company: Company = Depends(need_verified_company), db: Session = Depends(get_session)):
    stmt = (select(CandidateProfile).join(User, User.id == CandidateProfile.user_id)
            .where(CandidateProfile.searchable.is_(True), User.is_active.is_(True), User.role == "seeker"))
    if settings.require_email_verification:
        stmt = stmt.where(User.email_verified_at.is_not(None))
    for s in skills[:10]:
        stmt = stmt.where(CandidateProfile.skills.ilike(f"%|{s.strip()}|%"))
    if location:
        stmt = stmt.where(CandidateProfile.location == location)
    if min_years is not None:
        stmt = stmt.where(CandidateProfile.years_experience >= min_years)
    if max_salary:
        stmt = stmt.where(or_(CandidateProfile.desired_salary.is_(None), CandidateProfile.desired_salary <= max_salary))
    if q:
        for word in q.split()[:6]:
            like = f"%{word}%"
            in_resume = exists().where(Document.user_id == CandidateProfile.user_id, Document.kind == "resume",
                                       Document.archived.is_(False), Document.text.ilike(like))
            stmt = stmt.where(or_(CandidateProfile.headline.ilike(like), CandidateProfile.summary.ilike(like),
                                  CandidateProfile.skills.ilike(like), in_resume))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    # Highlighted profiles (a paid extra for job seekers) come first among the matches, then the most recently updated.
    hl = case((CandidateProfile.highlighted_until > utcnow(), 0), else_=1)
    rows = db.scalars(stmt.order_by(hl, CandidateProfile.updated_at.desc()).offset((page - 1) * 20).limit(20)).all()
    with_resume = set(db.scalars(select(Document.user_id).where(
        Document.user_id.in_([p.user_id for p in rows] or [-1]), Document.kind == "resume", Document.archived.is_(False))))
    return {"total": total, "page": page, "candidates": [candidate_card(p, p.user_id in with_resume) for p in rows]}


@router.get("/{user_id}")
def candidate(user_id: int, company: Company = Depends(need_verified_company), db: Session = Depends(get_session)):
    user, p = _visible(db, user_id)
    resume = _latest_resume(db, user_id)
    invites = db.scalars(select(CandidateInvite).options(joinedload(CandidateInvite.job))
                         .where(CandidateInvite.company_id == company.id, CandidateInvite.user_id == user_id)).all()
    applied = db.scalars(select(Job.id).join(Application, Application.job_id == Job.id)
                         .where(Job.company_id == company.id, Application.user_id == user_id)).all()
    return {**candidate_card(p, resume is not None), "summary": p.summary,
            "desired_setups": [x for x in (p.desired_setups or "").split("|") if x],
            "desired_job_types": [x for x in (p.desired_job_types or "").split("|") if x],
            "resume": {"id": resume.id, "filename": resume.filename, "size": resume.size} if resume else None,
            "invites": [{"job_id": i.job_id, "job": i.job.title, "status": i.status, "at": aware(i.created_at)} for i in invites],
            "applied_job_ids": list(applied)}


@router.get("/{user_id}/resume")
def candidate_resume(user_id: int, company: Company = Depends(need_verified_company), db: Session = Depends(get_session)):
    _visible(db, user_id)
    d = _latest_resume(db, user_id)
    if d is None:
        raise HTTPException(404, "This candidate hasn't uploaded a resume.")
    return file_response(d)


class InviteIn(BaseModel):
    job_id: int
    message: str = Field("", max_length=1000)


@router.post("/{user_id}/invite", status_code=201)
def invite(user_id: int, body: InviteIn, background: BackgroundTasks, sender: User = Depends(require_employer),
           company: Company = Depends(need_verified_company), db: Session = Depends(get_session)):
    require_verified(sender)
    user, p = _visible(db, user_id)
    job = db.scalar(select(Job).options(joinedload(Job.company)).where(Job.id == body.job_id, Job.company_id == company.id,
                                                                       Job.origin == "direct"))
    if job is None or not is_public(job):
        raise HTTPException(400, "Pick one of your live jobs.")
    if db.scalar(select(Application.id).where(Application.job_id == job.id, Application.user_id == user_id,
                                              Application.stage != "withdrawn")):
        raise HTTPException(409, "This candidate already applied to that job.")
    if db.scalar(select(CandidateInvite.id).where(CandidateInvite.job_id == job.id, CandidateInvite.user_id == user_id)):
        raise HTTPException(409, "You already invited this candidate to that job.")
    sent_today = db.scalar(select(func.count()).select_from(CandidateInvite).where(
        CandidateInvite.company_id == company.id, CandidateInvite.created_at > utcnow() - timedelta(days=1)))
    if sent_today >= INVITES_PER_DAY:
        raise HTTPException(429, f"You can send up to {INVITES_PER_DAY} invites a day. Try again tomorrow.")
    db.add(CandidateInvite(company_id=company.id, job_id=job.id, user_id=user_id, sent_by_id=sender.id,
                           message=body.message.strip()))
    db.add(Notification(user_id=user_id, text=f"{company.name} invited you to apply for {job.title}",
                        link="/seeker.html#invites"))
    msg = body.message.strip()
    queue_email(db, user.email, f"{company.name} invited you to apply: {job.title}",
                f"Hi {p.full_name},\n\n{company.name} found your profile on Sinag Jobs and invited you to apply for "
                f"{job.title} ({job.location}, {job.setup}).\n" + (f"\nTheir message:\n{msg}\n" if msg else "")
                + f"\nSee the job and apply: {settings.public_url}/#job-{job.id}\n\n"
                "Your contact details stay private unless you apply. To stop appearing in searches, switch off "
                f"\"Let verified employers find me\" in your profile: {settings.public_url}/seeker.html#profile\n")
    db.commit()
    flush_soon(background)
    return {"ok": True, "message": f"Invite sent to {p.full_name}."}


# ---------------------------------------------------------------------------
# Job seeker side
# ---------------------------------------------------------------------------

@seeker_router.get("")
def my_invites(user: User = Depends(require_seeker), db: Session = Depends(get_session)):
    rows = db.scalars(select(CandidateInvite).options(joinedload(CandidateInvite.job).joinedload(Job.company))
                      .where(CandidateInvite.user_id == user.id).order_by(CandidateInvite.id.desc())).all()
    return [{"id": i.id, "status": i.status, "message": i.message, "at": aware(i.created_at),
             "job": {"id": i.job.id, "title": i.job.title, "company": i.job.company.name, "location": i.job.location,
                     "setup": i.job.setup, "open": is_public(i.job), "company_slug": i.job.company.slug}} for i in rows]


@seeker_router.post("/{invite_id}/decline")
def decline(invite_id: int, user: User = Depends(require_seeker), db: Session = Depends(get_session)):
    i = db.get(CandidateInvite, invite_id)
    if i is None or i.user_id != user.id:
        raise HTTPException(404, "Invite not found.")
    if i.status == "sent":
        i.status = "declined"
        db.commit()
    return {"ok": True}
