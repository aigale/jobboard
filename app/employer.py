"""Employer features: company setup, job posting and scheduling, screening questions, the applicant pipeline."""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from .auth import require_employer, require_verified
from .common import (job_path, CATEGORIES, INDUSTRIES, JOB_TYPES, LEVELS, LOCATIONS, NOTIFY_STAGES, PERKS, SETUPS, SHIFTS,
                     STAGE_LABELS, STAGES, candidate_status, employer_job_state, text_to_html)
from .config import settings
from .crawler import slugify
from .db import get_session
from .mailer import flush_soon, queue_email
from .models import (Application, ApplicationEvent, Company, CompanyMember, Document, Job, JobQuestion,
                     Notification, User, aware, join_pipes, split_pipes, utcnow)
from .seeker import file_response

router = APIRouter(prefix="/api/employer", tags=["employers"])


# ---------------------------------------------------------------------------
# Company
# ---------------------------------------------------------------------------

class CompanyIn(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    industry: str
    website: str = Field("", max_length=300)
    location_text: str = Field("", max_length=200)
    about: str = Field("", max_length=5000)

    @field_validator("industry")
    @classmethod
    def _ind(cls, v):
        if v not in INDUSTRIES:
            raise ValueError("Choose an industry from the list.")
        return v

    @field_validator("website")
    @classmethod
    def _web(cls, v):
        v = v.strip()
        if v and not re.match(r"^https?://[^\s/$.?#].[^\s]*$", v):
            if re.match(r"^[^\s/]+\.[^\s]+$", v):
                return "https://" + v
            raise ValueError("Enter a website address like https://example.com.ph")
        return v


def company_out(c: Company) -> dict:
    return {"id": c.id, "name": c.name, "slug": c.slug, "industry": c.industry, "website": c.website,
            "location_text": c.location_text, "about": c.about, "approval": c.approval,
            "approval_note": c.approval_note}


def my_company(db: Session, user: User) -> Company | None:
    m = db.scalar(select(CompanyMember).where(CompanyMember.user_id == user.id))
    return m.company if m else None


def need_company(db: Session = Depends(get_session), user: User = Depends(require_employer)) -> Company:
    c = my_company(db, user)
    if c is None:
        raise HTTPException(409, "Set up your company profile first.")
    return c


@router.get("/company")
def get_company(user: User = Depends(require_employer), db: Session = Depends(get_session)):
    c = my_company(db, user)
    return {"company": company_out(c) if c else None}


@router.post("/company", status_code=201)
def create_company(body: CompanyIn, user: User = Depends(require_employer), db: Session = Depends(get_session)):
    if my_company(db, user):
        raise HTTPException(409, "Your account already has a company.")
    base = slugify(body.name) or "company"
    slug, n = base, 2
    while db.scalar(select(Company.id).where(Company.slug == slug)):
        slug, n = f"{base}-{n}", n + 1
    c = Company(slug=slug, name=body.name.strip(), source="direct", source_key="", careers_url=body.website or None,
                industry=body.industry, website=body.website or None, location_text=body.location_text.strip(),
                about=body.about.strip(), approval="pending", verified=False, active=True, options={})
    db.add(c)
    db.flush()
    db.add(CompanyMember(company_id=c.id, user_id=user.id, role="owner"))
    for admin in db.scalars(select(User).where(User.role == "admin", User.is_active.is_(True))):
        db.add(Notification(user_id=admin.id, text=f"New employer to review: {c.name}", link="/admin.html"))
        queue_email(db, admin.email, f"New employer to review: {c.name}",
                    f"{user.name} ({user.email}) registered {c.name} ({c.industry}).\n"
                    f"Website: {c.website or 'none given'}\n\nReview it: {settings.public_url}/admin.html\n")
    db.commit()
    return company_out(c)


@router.put("/company")
def update_company(body: CompanyIn, company: Company = Depends(need_company), db: Session = Depends(get_session)):
    if company.approval == "approved" and body.name.strip() != company.name:
        raise HTTPException(400, "Your company is already verified, so its name can't change here. Contact the site admin.")
    company.name = body.name.strip()
    company.industry, company.website = body.industry, body.website or None
    company.careers_url = body.website or None
    company.location_text, company.about = body.location_text.strip(), body.about.strip()
    if company.approval == "rejected":
        company.approval, company.approval_note = "pending", None    # resubmit for review
    db.commit()
    return company_out(company)


@router.get("/options")
def options():
    """Values for the job form's dropdowns."""
    from . import ai
    return {"ai_enabled": ai.enabled(), "locations": LOCATIONS, "setups": SETUPS, "shifts": SHIFTS, "levels": LEVELS, "job_types": JOB_TYPES,
            "categories": CATEGORIES, "industries": INDUSTRIES, "perks": PERKS, "stages": STAGES,
            "stage_labels": STAGE_LABELS}


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

class QuestionIn(BaseModel):
    text: str = Field(min_length=3, max_length=500)
    kind: str = "text"
    options: list[str] = Field(default_factory=list, max_length=10)
    required: bool = True
    knockout_answer: str | None = None

    @model_validator(mode="after")
    def _check(self):
        if self.kind not in ("text", "yesno", "choice"):
            raise ValueError("Question type must be text, yesno or choice.")
        self.options = [o.strip()[:200] for o in self.options if o.strip()]
        if self.kind == "yesno":
            self.options = ["Yes", "No"]
        if self.kind == "choice" and len(self.options) < 2:
            raise ValueError(f"Give at least two options for: {self.text}")
        if self.kind == "text":
            self.options, self.knockout_answer = [], None
        if self.knockout_answer and self.knockout_answer not in self.options:
            raise ValueError(f"The required answer must be one of the options for: {self.text}")
        return self


class JobIn(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    location: str
    city: str = Field("", max_length=120)
    setup: str
    shift: str = "Not stated"
    level: str
    category: str
    job_type: str = "Full-time"
    salary_min: int | None = Field(None, ge=0, le=3_000_000)
    salary_max: int | None = Field(None, ge=0, le=3_000_000)
    description: str = Field(min_length=50, max_length=20000)
    skills: list[str] = Field(default_factory=list, max_length=20)
    perks: list[str] = Field(default_factory=list)
    questions: list[QuestionIn] = Field(default_factory=list, max_length=10)
    publish_at: datetime | None = None
    expires_at: datetime | None = None
    action: str = "publish"             # draft | publish (publish_at in the future = scheduled)
    assessment_id: int | None = None
    assessment_auto: bool = False       # send the test to everyone who applies
    chat_screening: bool = False
    chat_topics: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def _check(self):
        for field, allowed in (("location", LOCATIONS), ("setup", SETUPS), ("shift", SHIFTS), ("level", LEVELS),
                               ("category", CATEGORIES), ("job_type", JOB_TYPES)):
            if getattr(self, field) not in allowed:
                raise ValueError(f"Choose a {field.replace('_', ' ')} from the list.")
        if self.action not in ("draft", "publish"):
            raise ValueError("Action must be draft or publish.")
        if self.salary_min and self.salary_max and self.salary_min > self.salary_max:
            raise ValueError("The minimum salary is higher than the maximum.")
        if self.salary_min and not self.salary_max:
            self.salary_max = self.salary_min
        if self.salary_max and not self.salary_min:
            self.salary_min = self.salary_max
        self.skills = list(dict.fromkeys(s.strip()[:60] for s in self.skills if s.strip() and "|" not in s))
        self.perks = [p for p in self.perks if p in PERKS]
        self.chat_topics = [t.strip()[:200] for t in self.chat_topics if t.strip()]
        if self.chat_screening and not self.chat_topics:
            raise ValueError("Add at least one topic for the screening chat, or turn it off.")
        if not self.assessment_id:
            self.assessment_auto = False
        for f in ("publish_at", "expires_at"):
            v = getattr(self, f)
            if v is not None and v.tzinfo is None:
                raise ValueError("Dates need a time zone.")
        if self.publish_at and self.expires_at and self.expires_at <= self.publish_at:
            raise ValueError("The closing date must be after the publish date.")
        return self


def _apply_job_fields(job: Job, body: JobIn) -> None:
    job.title = " ".join(body.title.split())
    job.location, job.city = body.location, body.city.strip() or None
    job.setup, job.shift, job.level, job.category, job.job_type = body.setup, body.shift, body.level, body.category, body.job_type
    job.employment_type = body.job_type
    job.salary_min, job.salary_max = body.salary_min, body.salary_max
    job.salary_original = f"PHP {body.salary_min:,}–{body.salary_max:,} / month" if body.salary_max else None
    job.description_text = body.description.strip()
    job.description_html = text_to_html(body.description)
    job.skills = join_pipes(body.skills)
    job.perks = join_pipes(body.perks)
    job.location_raw = ", ".join(x for x in (job.city, job.location) if x)
    job.content_hash = hashlib.sha256(f"{job.title}{job.description_text}".encode()).hexdigest()
    job.expires_at = body.expires_at
    job.assessment_id, job.assessment_auto = body.assessment_id, body.assessment_auto
    job.chat_screening, job.chat_topics = body.chat_screening, body.chat_topics
    job.questions.clear()
    for i, q in enumerate(body.questions):
        job.questions.append(JobQuestion(position=i, text=q.text.strip(), kind=q.kind, options=q.options,
                                         required=q.required, knockout_answer=q.knockout_answer))


def _publish(job: Job, body: JobIn) -> None:
    now = utcnow()
    if body.action == "draft":
        job.status = "draft"
        job.publish_at = body.publish_at
        return
    was_live = job.status == "open" and (job.publish_at is None or aware(job.publish_at) <= now)
    job.status = "open"
    job.closed_at = None
    if body.publish_at and body.publish_at > now:
        job.publish_at = job.posted_at = body.publish_at
    elif not was_live:
        job.publish_at = None
        job.posted_at = now


def job_admin_out(db: Session, job: Job) -> dict:
    counts = dict(db.execute(select(Application.stage, func.count()).where(Application.job_id == job.id)
                             .group_by(Application.stage)).all())
    active = sum(v for k, v in counts.items() if k not in ("withdrawn", "rejected"))
    return {
        "id": job.id, "title": job.title, "state": employer_job_state(job), "status": job.status,
        "location": job.location, "city": job.city, "setup": job.setup, "shift": job.shift, "level": job.level,
        "category": job.category, "job_type": job.job_type, "salary_min": job.salary_min,
        "salary_max": job.salary_max, "description": job.description_text, "skills": job.skill_list,
        "perks": job.perk_list, "publish_at": aware(job.publish_at), "expires_at": aware(job.expires_at),
        "posted_at": aware(job.posted_at), "views": job.view_count, "applicants": active,
        "paid_via": job.paid_via, "featured_until": aware(job.featured_until),
        "new_applicants": counts.get("applied", 0),
        "assessment_id": job.assessment_id, "assessment_auto": job.assessment_auto,
        "chat_screening": job.chat_screening, "chat_topics": job.chat_topics or [],
        "questions": [{"id": q.id, "text": q.text, "kind": q.kind, "options": q.options, "required": q.required,
                       "knockout_answer": q.knockout_answer} for q in job.questions],
    }


def _check_assessment(db: Session, company: Company, body: JobIn) -> None:
    if body.assessment_id:
        from .models import Assessment
        a = db.get(Assessment, body.assessment_id)
        if a is None or a.company_id != company.id:
            raise HTTPException(422, "Pick one of your company's tests.")


def _own_job(db: Session, company: Company, job_id: int) -> Job:
    job = db.scalar(select(Job).options(joinedload(Job.company), joinedload(Job.questions))
                    .where(Job.id == job_id, Job.company_id == company.id, Job.origin == "direct"))
    if job is None:
        raise HTTPException(404, "Job not found.")
    return job


@router.get("/jobs")
def list_my_jobs(company: Company = Depends(need_company), db: Session = Depends(get_session)):
    jobs = db.scalars(select(Job).options(joinedload(Job.company), joinedload(Job.questions))
                      .where(Job.company_id == company.id, Job.origin == "direct")
                      .order_by(Job.id.desc())).unique().all()
    return [job_admin_out(db, j) for j in jobs]


@router.get("/jobs/{job_id}")
def get_my_job(job_id: int, company: Company = Depends(need_company), db: Session = Depends(get_session)):
    return job_admin_out(db, _own_job(db, company, job_id))


@router.post("/jobs", status_code=201)
def create_job(body: JobIn, user: User = Depends(require_employer), company: Company = Depends(need_company),
               db: Session = Depends(get_session)):
    if body.action == "publish":
        require_verified(user)
    open_count = db.scalar(select(func.count()).select_from(Job).where(
        Job.company_id == company.id, Job.origin == "direct", Job.status != "closed"))
    if open_count >= 200:
        raise HTTPException(422, "You have 200 open or draft jobs. Close some before adding more.")
    _check_assessment(db, company, body)
    now = utcnow()
    job = Job(company_id=company.id, external_id=f"direct-{secrets.token_hex(8)}", origin="direct",
              ph_eligibility="ph_location", easy_apply=True, created_by_id=user.id, posted_at=now,
              first_seen_at=now, last_seen_at=now, url="", status="draft")
    db.add(job)
    _apply_job_fields(job, body)
    _publish(job, body)
    db.flush()
    job.url = job.apply_url = f"{settings.public_url}{job_path(job)}"
    _charge_or_draft(db, company, job)
    db.commit()
    return job_admin_out(db, job)


def _charge_or_draft(db: Session, company: Company, job: Job) -> None:
    """Publishing uses the plan, the free allowance or a job credit. Without any, the job stays a draft."""
    from .billing import PaymentRequired, charge_publish
    try:
        charge_publish(db, company, job)
    except PaymentRequired as exc:
        job.status, job.publish_at = "draft", None
        db.commit()
        raise HTTPException(402, {"message": exc.detail, "job_id": job.id})


@router.put("/jobs/{job_id}")
def update_job(job_id: int, body: JobIn, user: User = Depends(require_employer), company: Company = Depends(need_company),
               db: Session = Depends(get_session)):
    if body.action == "publish":
        require_verified(user)
    job = _own_job(db, company, job_id)
    _check_assessment(db, company, body)
    _apply_job_fields(job, body)
    _publish(job, body)
    job.url = job.apply_url = f"{settings.public_url}{job_path(job)}"
    _charge_or_draft(db, company, job)
    db.commit()
    return job_admin_out(db, job)


@router.post("/jobs/{job_id}/close")
def close_job(job_id: int, company: Company = Depends(need_company), db: Session = Depends(get_session)):
    job = _own_job(db, company, job_id)
    job.status, job.closed_at = "closed", utcnow()
    db.commit()
    return job_admin_out(db, job)


@router.delete("/jobs/{job_id}")
def delete_job(job_id: int, company: Company = Depends(need_company), db: Session = Depends(get_session)):
    job = _own_job(db, company, job_id)
    if job.status != "draft":
        raise HTTPException(400, "Only drafts can be deleted. Close the job instead; applicants keep their history.")
    db.delete(job)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Applicant pipeline
# ---------------------------------------------------------------------------

def _own_application(db: Session, company: Company, app_id: int) -> Application:
    a = db.scalar(select(Application).options(joinedload(Application.job), joinedload(Application.events))
                  .where(Application.id == app_id))
    if a is None or a.job.company_id != company.id:
        raise HTTPException(404, "Application not found.")
    return a


def card_out(a: Application) -> dict:
    snap = a.profile_snapshot or {}
    return {"id": a.id, "name": snap.get("full_name") or "Applicant", "headline": snap.get("headline", ""),
            "location": snap.get("location", ""), "score": a.score, "flags": (a.score_details or {}).get("flags", []),
            "stage": a.stage, "tags": split_pipes(a.tags), "viewed": a.viewed_at is not None, "priority": bool(a.priority),
            "applied_at": aware(a.created_at)}


@router.get("/jobs/{job_id}/applications")
def pipeline(job_id: int, company: Company = Depends(need_company), db: Session = Depends(get_session)):
    job = _own_job(db, company, job_id)
    apps = db.scalars(select(Application).where(Application.job_id == job.id, Application.stage != "withdrawn")
                      .order_by(Application.priority.desc(), Application.score.desc(), Application.created_at)).all()
    from .models import AssessmentAttempt, ScreeningChat
    ids = [a.id for a in apps] or [-1]
    tests: dict[int, AssessmentAttempt] = {}
    for att in db.scalars(select(AssessmentAttempt).where(AssessmentAttempt.application_id.in_(ids))
                          .order_by(AssessmentAttempt.id)):
        tests[att.application_id] = att
    chats = {c.application_id: c.status for c in db.scalars(select(ScreeningChat).where(ScreeningChat.application_id.in_(ids)))}
    cards = []
    for a in apps:
        c = card_out(a)
        t = tests.get(a.id)
        c["test"] = None if t is None else {"score": t.score, "passed": t.passed, "done": t.submitted_at is not None}
        c["chat"] = chats.get(a.id)
        cards.append(c)
    return {"job": {"id": job.id, "title": job.title, "state": employer_job_state(job)},
            "stages": [{"key": s, "label": STAGE_LABELS[s]} for s in STAGES], "applications": cards}


@router.get("/applications/{app_id}")
def application_detail(app_id: int, user: User = Depends(require_employer), company: Company = Depends(need_company),
                       db: Session = Depends(get_session)):
    a = _own_application(db, company, app_id)
    if a.viewed_at is None and a.stage != "withdrawn":
        a.viewed_at = utcnow()
        db.add(ApplicationEvent(application_id=a.id, kind="viewed", actor_id=user.id, visible_to_candidate=True))
        db.commit()
        db.refresh(a)
    docs = db.scalars(select(Document).where(Document.id.in_(a.document_ids or [-1]))).all()
    from .models import ScreeningChat
    from .screening import attempts_for, chat_out, employer_attempt_detail
    chat = db.scalar(select(ScreeningChat).where(ScreeningChat.application_id == a.id))
    return {
        "tests": [employer_attempt_detail(t) for t in attempts_for(db, a.id)],
        "chat": chat_out(chat) if chat else None, "ai_summary": a.ai_summary,
        **card_out(a), "job": {"id": a.job.id, "title": a.job.title},
        "profile": a.profile_snapshot, "cover_letter": a.cover_letter, "answers": a.answers,
        "score_details": a.score_details, "candidate_status": candidate_status(a),
        "documents": [{"id": d.id, "kind": d.kind, "filename": d.filename, "size": d.size} for d in docs],
        "history": [{"at": aware(e.at), "kind": e.kind, "from": e.from_stage, "to": e.to_stage, "note": e.note}
                    for e in a.events],
    }


class ApplicationPatch(BaseModel):
    stage: str | None = None
    tags: list[str] | None = Field(None, max_length=10)
    message: str | None = Field(None, max_length=2000)   # optional note to the candidate with a stage change

    @field_validator("stage")
    @classmethod
    def _stage(cls, v):
        if v is not None and v not in STAGES:
            raise ValueError("Unknown stage.")
        return v


@router.patch("/applications/{app_id}")
def update_application(app_id: int, body: ApplicationPatch, background: BackgroundTasks,
                       user: User = Depends(require_employer), company: Company = Depends(need_company),
                       db: Session = Depends(get_session)):
    a = _own_application(db, company, app_id)
    if a.stage == "withdrawn":
        raise HTTPException(400, "The candidate withdrew this application.")
    if body.tags is not None:
        a.tags = join_pipes([t[:30] for t in body.tags])
    if body.stage and body.stage != a.stage:
        notify = body.stage in NOTIFY_STAGES
        db.add(ApplicationEvent(application_id=a.id, kind="stage", from_stage=a.stage, to_stage=body.stage,
                                actor_id=user.id, visible_to_candidate=notify,
                                note=(body.message or "").strip() or None))
        if a.viewed_at is None:
            a.viewed_at = utcnow()
        a.stage = body.stage
        if notify:
            label = candidate_status(a)
            db.add(Notification(user_id=a.user_id, text=f"{a.job.title} at {company.name}: {label}",
                                link="/seeker.html#applications"))
            msg = (body.message or "").strip()
            queue_email(db, a.user.email, f"Update on your application: {a.job.title}",
                        f"Hi {(a.profile_snapshot or {}).get('full_name', '')},\n\n"
                        f"Your application for {a.job.title} at {company.name} is now: {label}.\n"
                        + (f"\nMessage from {company.name}:\n{msg}\n" if msg else "")
                        + f"\nSee all your applications: {settings.public_url}/seeker.html#applications\n")
    db.commit()
    flush_soon(background)
    db.refresh(a)
    return card_out(a)


class NoteIn(BaseModel):
    text: str = Field(min_length=1, max_length=5000)


@router.post("/applications/{app_id}/notes", status_code=201)
def add_note(app_id: int, body: NoteIn, user: User = Depends(require_employer),
             company: Company = Depends(need_company), db: Session = Depends(get_session)):
    a = _own_application(db, company, app_id)
    db.add(ApplicationEvent(application_id=a.id, kind="note", note=body.text.strip(), actor_id=user.id,
                            visible_to_candidate=False))
    db.commit()
    return {"ok": True}


@router.get("/applications/{app_id}/documents/{doc_id}")
def applicant_document(app_id: int, doc_id: int, company: Company = Depends(need_company),
                       db: Session = Depends(get_session)):
    a = _own_application(db, company, app_id)
    if doc_id not in (a.document_ids or []):
        raise HTTPException(404, "File not found.")
    d = db.get(Document, doc_id)
    if d is None:
        raise HTTPException(404, "The candidate deleted this file.")
    return file_response(d)
