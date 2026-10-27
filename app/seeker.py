"""Job seeker features: profile, documents, Easy Apply, application tracking, matches, alerts, notifications."""
from __future__ import annotations

import secrets
from datetime import timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session, joinedload

from . import files
from .auth import current_user, require_seeker, require_verified, verify_password
from .common import (CATEGORIES, JOB_TYPES, LOCATIONS, SETUPS, candidate_status, is_public, public_job_filter)
from .config import settings
from .db import get_session
from .mailer import flush_soon, queue_email
from .matching import applicant_score, check_answers, match_score
from .models import (AlertSubscription, Application, ApplicationEvent, CandidateProfile, Company, CompanyMember,
                     Document, Job, Notification, User, aware, join_pipes, split_pipes, utcnow)

router = APIRouter(prefix="/api/me", tags=["job seekers"])


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

class ProfileIn(BaseModel):
    full_name: str = Field(min_length=2, max_length=200)
    headline: str = Field("", max_length=200)
    phone: str = Field("", max_length=40)
    location: str = ""
    summary: str = Field("", max_length=5000)
    skills: list[str] = Field(default_factory=list, max_length=40)
    years_experience: int | None = Field(None, ge=0, le=60)
    desired_salary: int | None = Field(None, ge=0, le=3_000_000)
    desired_setups: list[str] = Field(default_factory=list)
    desired_job_types: list[str] = Field(default_factory=list)
    desired_categories: list[str] = Field(default_factory=list)
    searchable: bool = False

    @field_validator("location")
    @classmethod
    def _loc(cls, v):
        if v and v not in LOCATIONS:
            raise ValueError("Choose a location from the list.")
        return v

    @field_validator("skills")
    @classmethod
    def _skills(cls, v):
        return [s.strip()[:60] for s in v if s.strip() and "|" not in s]

    @field_validator("desired_setups")
    @classmethod
    def _setups(cls, v):
        return [x for x in v if x in SETUPS]

    @field_validator("desired_job_types")
    @classmethod
    def _types(cls, v):
        return [x for x in v if x in JOB_TYPES]

    @field_validator("desired_categories")
    @classmethod
    def _cats(cls, v):
        return [x for x in v if x in CATEGORIES]


def profile_out(p: CandidateProfile) -> dict:
    return {
        "full_name": p.full_name, "headline": p.headline, "phone": p.phone, "location": p.location,
        "summary": p.summary, "skills": p.skill_list, "years_experience": p.years_experience,
        "desired_salary": p.desired_salary, "desired_setups": split_pipes(p.desired_setups),
        "desired_job_types": split_pipes(p.desired_job_types),
        "desired_categories": split_pipes(p.desired_categories), "searchable": p.searchable,
        "complete": bool(p.full_name and p.headline and p.skill_list),
        "priority_credits": p.priority_credits or 0,
    }


def _profile(db: Session, user: User) -> CandidateProfile:
    p = db.get(CandidateProfile, user.id)
    if p is None:
        p = CandidateProfile(user_id=user.id, full_name=user.name)
        db.add(p)
        db.flush()
    return p


@router.get("/profile")
def get_profile(user: User = Depends(require_seeker), db: Session = Depends(get_session)):
    p = _profile(db, user)
    db.commit()
    return profile_out(p)


@router.put("/profile")
def put_profile(body: ProfileIn, user: User = Depends(require_seeker), db: Session = Depends(get_session)):
    p = _profile(db, user)
    p.full_name, p.headline, p.phone, p.location = body.full_name.strip(), body.headline.strip(), body.phone.strip(), body.location
    p.summary = body.summary.strip()
    p.skills = join_pipes(body.skills)
    p.years_experience, p.desired_salary = body.years_experience, body.desired_salary
    p.desired_setups = join_pipes(body.desired_setups)
    p.desired_job_types = join_pipes(body.desired_job_types)
    p.desired_categories = join_pipes(body.desired_categories)
    p.searchable = body.searchable
    user.name = p.full_name
    db.commit()
    return profile_out(p)


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

def doc_out(d: Document) -> dict:
    return {"id": d.id, "kind": d.kind, "filename": d.filename, "size": d.size,
            "content_type": d.content_type, "created_at": aware(d.created_at), "has_text": bool(d.text)}


@router.get("/documents")
def list_documents(user: User = Depends(require_seeker), db: Session = Depends(get_session)):
    docs = db.scalars(select(Document).where(Document.user_id == user.id, Document.archived.is_(False))
                      .order_by(Document.id.desc())).all()
    return [doc_out(d) for d in docs]


@router.post("/documents", status_code=201)
async def upload_document(file: UploadFile = File(...), kind: str = Form("resume"),
                          user: User = Depends(require_seeker), db: Session = Depends(get_session)):
    if kind not in files.KINDS:
        raise HTTPException(422, "Choose resume, certificate, portfolio or other.")
    count = db.scalar(select(func.count()).select_from(Document).where(
        Document.user_id == user.id, Document.archived.is_(False)))
    if count >= settings.max_documents_per_user:
        raise HTTPException(422, f"You can keep up to {settings.max_documents_per_user} files. Delete one first.")
    data = await file.read(settings.max_upload_mb * 1024 * 1024 + 1)
    name = files.safe_download_name(file.filename or "document")
    ext, ctype = files.validate(name, data)
    stored = files.save(user.id, ext, data)
    d = Document(user_id=user.id, kind=kind, filename=name, stored_name=stored, content_type=ctype,
                 size=len(data), text=files.extract_text(ext, data))
    db.add(d)
    db.commit()
    return doc_out(d)


@router.delete("/documents/{doc_id}")
def delete_document(doc_id: int, user: User = Depends(require_seeker), db: Session = Depends(get_session)):
    d = db.get(Document, doc_id)
    if d is None or d.user_id != user.id or d.archived:
        raise HTTPException(404, "File not found.")
    used = any(doc_id in (a.document_ids or []) for a in db.scalars(
        select(Application).where(Application.user_id == user.id, Application.stage != "withdrawn")))
    if used:
        d.archived = True       # employers who received it can still open it
    else:
        files.remove(d.stored_name)
        db.delete(d)
    db.commit()
    return {"ok": True}


@router.get("/documents/{doc_id}/file")
def download_document(doc_id: int, user: User = Depends(require_seeker), db: Session = Depends(get_session)):
    d = db.get(Document, doc_id)
    if d is None or d.user_id != user.id:
        raise HTTPException(404, "File not found.")
    return file_response(d)


def file_response(d: Document) -> FileResponse:
    p = files.path_for(d.stored_name)
    if not p.exists():
        raise HTTPException(404, "File not found.")
    return FileResponse(p, media_type=d.content_type, filename=d.filename,
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})


# ---------------------------------------------------------------------------
# Easy Apply
# ---------------------------------------------------------------------------

class ApplyIn(BaseModel):
    document_ids: list[int] = Field(default_factory=list, max_length=10)
    cover_letter: str = Field("", max_length=5000)
    answers: dict[int, str] = Field(default_factory=dict)
    priority: bool = False          # use one priority application (paid extra)


apply_router = APIRouter(tags=["job seekers"])


@apply_router.post("/api/jobs/{job_id}/apply", status_code=201)
def apply(job_id: int, body: ApplyIn, background: BackgroundTasks,
          user: User = Depends(require_seeker), db: Session = Depends(get_session)):
    require_verified(user)
    job = db.scalar(select(Job).options(joinedload(Job.company), joinedload(Job.questions)).where(Job.id == job_id))
    if job is None or not is_public(job):
        raise HTTPException(404, "This job is no longer open.")
    if not job.easy_apply:
        raise HTTPException(400, "This job takes applications on the company's own site.")
    existing = db.scalar(select(Application).where(Application.job_id == job.id, Application.user_id == user.id))
    if existing and existing.stage != "withdrawn":
        raise HTTPException(409, "You already applied to this job. Track it under My applications.")

    p = _profile(db, user)
    if body.priority and (p.priority_credits or 0) < 1:
        raise HTTPException(402, "You have no priority applications left. Get more under Extras, or apply without one.")
    if not (p.full_name and p.headline):
        raise HTTPException(422, "Add your name and a headline to your profile before applying.")

    docs = db.scalars(select(Document).where(Document.id.in_(body.document_ids or [-1]),
                                             Document.user_id == user.id, Document.archived.is_(False))).all()
    if len(docs) != len(set(body.document_ids)):
        raise HTTPException(422, "One of the files you picked is no longer available. Refresh and try again.")
    resumes = [d for d in docs if d.kind == "resume"]
    if not resumes:
        raise HTTPException(422, "Attach a resume. You can upload one in this form.")

    answers: dict[int, str] = {}
    answer_rows = []
    for q in job.questions:
        a = (body.answers.get(q.id) or "").strip()[:2000]
        if q.required and not a:
            raise HTTPException(422, f"Answer this question: {q.text}")
        if a and q.kind == "yesno" and a not in ("Yes", "No"):
            raise HTTPException(422, f"Answer Yes or No: {q.text}")
        if a and q.kind == "choice" and a not in (q.options or []):
            raise HTTPException(422, f"Pick one of the options for: {q.text}")
        answers[q.id] = a
        answer_rows.append({"question_id": q.id, "question": q.text, "answer": a})

    failed = check_answers(job.questions, answers)
    resume_text = "\n".join(d.text or "" for d in resumes)
    sc = applicant_score(job, p.skill_list, p.years_experience, p.desired_salary, resume_text, True, failed)

    snapshot = {**profile_out(p), "email": user.email}
    snapshot.pop("searchable", None)
    snapshot.pop("priority_credits", None)
    if existing:          # re-applying after withdrawing
        a = existing
        a.stage, a.viewed_at, a.tags = "applied", None, ""
    else:
        a = Application(job_id=job.id, user_id=user.id)
        db.add(a)
    a.cover_letter = body.cover_letter.strip()
    a.answers = answer_rows
    a.profile_snapshot = snapshot
    a.document_ids = [d.id for d in docs]
    a.score, a.score_details = sc.score, sc.as_dict()
    a.created_at = utcnow()
    a.priority = bool(body.priority)
    if a.priority:
        p.priority_credits -= 1
    db.flush()
    db.add(ApplicationEvent(application_id=a.id, kind="applied", to_stage="applied", actor_id=user.id,
                            visible_to_candidate=True))

    # Phase 2 follow-ups: invite bookkeeping, automatic skills test, screening chat
    from .models import Assessment, CandidateInvite, ScreeningChat
    from .screening import send_test, start_chat
    inv = db.scalar(select(CandidateInvite).where(CandidateInvite.job_id == job.id, CandidateInvite.user_id == user.id))
    if inv is not None:
        inv.status = "applied"
    if job.assessment_id and job.assessment_auto:
        test = db.get(Assessment, job.assessment_id)
        if test is not None:
            send_test(db, a, test, None)
    if db.scalar(select(ScreeningChat.id).where(ScreeningChat.application_id == a.id)) is None:
        start_chat(db, a, job, job.company)

    for m in db.scalars(select(CompanyMember).where(CompanyMember.company_id == job.company_id)):
        db.add(Notification(user_id=m.user_id, text=f"New {'priority ' if a.priority else ''}applicant for {job.title}: {p.full_name}",
                            link=f"/employer.html#pipeline-{job.id}"))
        queue_email(db, m.user.email, f"New applicant: {job.title}",
                    f"{p.full_name} ({p.headline}) applied to {job.title}.\nMatch score: {sc.score}/100.\n\n"
                    f"Review the application: {settings.public_url}/employer.html#pipeline-{job.id}\n")
    db.commit()
    flush_soon(background)
    from .analytics import record
    record(db, "application", str(job.id))
    return {"ok": True, "application_id": a.id, "message": f"Application sent to {job.company.name}."}


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------

def application_out(a: Application) -> dict:
    return {
        "id": a.id, "status": candidate_status(a), "stage": a.stage, "applied_at": aware(a.created_at),
        "updated_at": aware(a.updated_at),
        "job": {"id": a.job.id, "title": a.job.title, "company": a.job.company.name, "location": a.job.location,
                "setup": a.job.setup, "open": is_public(a.job)},
        "timeline": [{"at": aware(e.at), "kind": e.kind, "status": _event_label(e)}
                     for e in a.events if e.visible_to_candidate],
    }


def _with_followups(db: Session, out: dict) -> dict:
    from .models import ScreeningChat
    from .screening import attempt_summary, attempts_for
    out["tests"] = [attempt_summary(t) for t in attempts_for(db, out["id"])]
    chat = db.scalar(select(ScreeningChat).where(ScreeningChat.application_id == out["id"]))
    out["chat"] = chat.status if chat else None
    from .models import Message
    out["messages"] = db.scalar(select(func.count()).select_from(Message).where(Message.application_id == out["id"])) or 0
    out["unread"] = db.scalar(select(func.count()).select_from(Message).where(
        Message.application_id == out["id"], Message.side == "employer", Message.read_at.is_(None))) or 0
    return out


def _event_label(e: ApplicationEvent) -> str:
    if e.kind == "applied":
        return "Application sent"
    if e.kind == "viewed":
        return "Viewed by the employer"
    if e.kind == "withdrawn":
        return "You withdrew"
    if e.kind in ("assessment", "chat"):
        return e.note or "Updated"
    return {"shortlisted": "Shortlisted", "interview": "Invited to interview", "offer": "Offer stage",
            "hired": "Hired", "rejected": "Not selected"}.get(e.to_stage or "", "Updated")


@router.get("/applications")
def my_applications(user: User = Depends(require_seeker), db: Session = Depends(get_session)):
    apps = db.scalars(select(Application).options(joinedload(Application.job).joinedload(Job.company),
                                                  joinedload(Application.events))
                      .where(Application.user_id == user.id).order_by(Application.created_at.desc())).unique().all()
    return [_with_followups(db, application_out(a)) for a in apps]


@router.post("/applications/{app_id}/withdraw")
def withdraw(app_id: int, user: User = Depends(require_seeker), db: Session = Depends(get_session)):
    a = db.get(Application, app_id)
    if a is None or a.user_id != user.id:
        raise HTTPException(404, "Application not found.")
    if a.stage in ("withdrawn", "hired", "rejected"):
        raise HTTPException(400, "This application can't be withdrawn now.")
    db.add(ApplicationEvent(application_id=a.id, kind="withdrawn", from_stage=a.stage, to_stage="withdrawn",
                            actor_id=user.id, visible_to_candidate=True))
    a.stage = "withdrawn"
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------

@router.get("/matches")
def matches(limit: int = 20, user: User = Depends(require_seeker), db: Session = Depends(get_session)):
    p = _profile(db, user)
    db.commit()
    if not (p.skill_list or split_pipes(p.desired_categories)):
        return {"ready": False, "jobs": [],
                "message": "Add your skills and the kinds of jobs you want to your profile to see matches."}
    applied = set(db.scalars(select(Application.job_id).where(Application.user_id == user.id)))
    jobs = db.scalars(select(Job).join(Company).options(joinedload(Job.company)).where(public_job_filter())
                      .order_by(Job.posted_at.desc()).limit(1000)).all()
    scored = []
    for j in jobs:
        if j.id in applied:
            continue
        s = match_score(p, j)
        if s.score >= 40:
            scored.append((s, j))
    scored.sort(key=lambda x: (-x[0].score, -aware(x[1].posted_at).timestamp()))
    from .api import summary_dict
    return {"ready": True, "jobs": [{**summary_dict(j), "match": s.as_dict()} for s, j in scored[:min(limit, 50)]]}


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

class MyAlertIn(BaseModel):
    category: str | None = None
    location: str | None = None
    keywords: str | None = Field(None, max_length=200)
    min_pay: int | None = Field(None, ge=0, le=3_000_000)
    frequency: str = "daily"
    use_profile: bool = False

    @field_validator("frequency")
    @classmethod
    def _freq(cls, v):
        if v not in ("daily", "instant"):
            raise ValueError("Choose daily or instant.")
        return v

    @field_validator("category", "location", "keywords")
    @classmethod
    def _blank(cls, v):
        return (v.strip() or None) if v else None


def alert_out(a: AlertSubscription) -> dict:
    return {"id": a.id, "category": a.category, "location": a.location, "keywords": a.keywords,
            "min_pay": a.min_pay, "frequency": a.frequency, "use_profile": a.use_profile, "active": a.active,
            "confirmed": a.confirmed,
            "last_sent_at": aware(a.last_sent_at)}


@router.get("/alerts")
def my_alerts(user: User = Depends(current_user), db: Session = Depends(get_session)):
    rows = db.scalars(select(AlertSubscription).where(
        (AlertSubscription.user_id == user.id) | (AlertSubscription.email == user.email))
        .order_by(AlertSubscription.id)).all()
    for r in rows:             # claim alerts made before the account existed
        r.user_id = user.id
    db.commit()
    return [alert_out(r) for r in rows if r.active]


@router.post("/alerts", status_code=201)
def add_alert(body: MyAlertIn, user: User = Depends(current_user), db: Session = Depends(get_session)):
    count = db.scalar(select(func.count()).select_from(AlertSubscription).where(
        AlertSubscription.user_id == user.id, AlertSubscription.active.is_(True)))
    if count >= 10:
        raise HTTPException(422, "You can have up to 10 alerts. Delete one first.")
    # Reuse an inactive or matching row so the unique (email, category, location) rule never blocks you.
    a = db.scalar(select(AlertSubscription).where(
        AlertSubscription.email == user.email,
        AlertSubscription.category.is_(None) if body.category is None else AlertSubscription.category == body.category,
        AlertSubscription.location.is_(None) if body.location is None else AlertSubscription.location == body.location))
    if a is None:
        a = AlertSubscription(email=user.email, category=body.category, location=body.location)
        db.add(a)
    a.user_id, a.keywords, a.min_pay, a.frequency, a.use_profile = user.id, body.keywords, body.min_pay, body.frequency, body.use_profile
    a.active = True
    a.confirmed = user.email_verified_at is not None or not settings.require_email_verification
    a.unsubscribe_token = a.unsubscribe_token or secrets.token_urlsafe(24)
    a.last_sent_at = a.last_sent_at or utcnow()
    db.commit()
    return alert_out(a)


@router.delete("/alerts/{alert_id}")
def delete_alert(alert_id: int, user: User = Depends(current_user), db: Session = Depends(get_session)):
    a = db.get(AlertSubscription, alert_id)
    if a is None or a.user_id != user.id:
        raise HTTPException(404, "Alert not found.")
    a.active = False
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Notifications and account
# ---------------------------------------------------------------------------

@router.get("/notifications")
def notifications(user: User = Depends(current_user), db: Session = Depends(get_session)):
    rows = db.scalars(select(Notification).where(Notification.user_id == user.id)
                      .order_by(Notification.id.desc()).limit(50)).all()
    unread = db.scalar(select(func.count()).select_from(Notification).where(
        Notification.user_id == user.id, Notification.read_at.is_(None)))
    return {"unread": unread, "items": [{"id": n.id, "at": aware(n.at), "text": n.text, "link": n.link,
                                         "read": n.read_at is not None} for n in rows]}


@router.post("/notifications/read")
def read_notifications(user: User = Depends(current_user), db: Session = Depends(get_session)):
    db.execute(update(Notification).where(Notification.user_id == user.id, Notification.read_at.is_(None))
               .values(read_at=utcnow()))
    db.commit()
    return {"ok": True}


class PushIn(BaseModel):
    endpoint: str = Field(max_length=1000)
    keys: dict[str, str]

    @field_validator("endpoint")
    @classmethod
    def _https(cls, v):
        if not v.startswith("https://"):
            raise ValueError("Push endpoints must use https.")
        return v


@router.post("/push", status_code=201)
def push_subscribe(body: PushIn, user: User = Depends(current_user), db: Session = Depends(get_session)):
    from .models import PushSubscription
    if not (body.keys.get("p256dh") and body.keys.get("auth")):
        raise HTTPException(422, "This browser sent an incomplete push subscription.")
    sub = db.scalar(select(PushSubscription).where(PushSubscription.endpoint == body.endpoint))
    if sub is None:
        sub = PushSubscription(endpoint=body.endpoint)
        db.add(sub)
    sub.user_id, sub.p256dh, sub.auth = user.id, body.keys["p256dh"][:200], body.keys["auth"][:100]
    db.commit()
    return {"ok": True, "message": "Push notifications are on for this browser."}


class PushOffIn(BaseModel):
    endpoint: str


@router.post("/push/off")
def push_unsubscribe(body: PushOffIn, user: User = Depends(current_user), db: Session = Depends(get_session)):
    from .models import PushSubscription
    db.execute(delete(PushSubscription).where(PushSubscription.endpoint == body.endpoint,
                                              PushSubscription.user_id == user.id))
    db.commit()
    return {"ok": True}


class DeleteAccountIn(BaseModel):
    password: str = ""
    confirm_email: str = ""         # accounts made with Google/Facebook/LinkedIn have no password: type the email instead


class LangIn(BaseModel):
    lang: str = Field(pattern="^(en|tl|ceb)$")


@router.put("/lang")
def set_lang(body: LangIn, user: User = Depends(current_user), db: Session = Depends(get_session)):
    """Remember the site language (English, Tagalog or Cebuano) across devices."""
    user.lang = body.lang
    db.commit()
    return {"ok": True}


class DeviceIn(BaseModel):
    token: str = Field(min_length=20, max_length=500)
    platform: str = Field(pattern="^(android|ios)$")


@router.post("/devices", status_code=201)
def register_device(body: DeviceIn, user: User = Depends(current_user), db: Session = Depends(get_session)):
    """The phone app calls this after the person allows notifications."""
    from .models import DeviceToken
    d = db.scalar(select(DeviceToken).where(DeviceToken.token == body.token))
    if d is None:
        d = DeviceToken(token=body.token, platform=body.platform, user_id=user.id)
        db.add(d)
    d.user_id, d.platform, d.last_seen_at = user.id, body.platform, utcnow()    # a shared phone follows whoever signed in
    db.commit()
    return {"ok": True}


class DeviceOffIn(BaseModel):
    token: str = Field(min_length=1, max_length=500)


@router.post("/devices/off")
def unregister_device(body: DeviceOffIn, user: User = Depends(current_user), db: Session = Depends(get_session)):
    from .models import DeviceToken
    db.execute(delete(DeviceToken).where(DeviceToken.token == body.token, DeviceToken.user_id == user.id))
    db.commit()
    return {"ok": True}


@router.post("/delete-account")
def delete_account(body: DeleteAccountIn, response: Response, user: User = Depends(current_user),
                   db: Session = Depends(get_session)):
    """Removes the account, profile, files, applications and alerts (Data Privacy Act: right to erasure)."""
    if user.password_hash:
        if not verify_password(body.password, user.password_hash):
            raise HTTPException(400, "Your password isn't right.")
    elif body.confirm_email.strip().lower() != user.email:
        raise HTTPException(400, "Type your email address exactly to confirm.")
    if user.role == "admin":
        raise HTTPException(400, "Admin accounts are removed with the command line tool.")
    for d in db.scalars(select(Document).where(Document.user_id == user.id)):
        files.remove(d.stored_name)
    # An employer leaving: if nobody else manages the company, take its job posts down.
    for m in db.scalars(select(CompanyMember).where(CompanyMember.user_id == user.id)):
        others = db.scalar(select(func.count()).select_from(CompanyMember).where(
            CompanyMember.company_id == m.company_id, CompanyMember.user_id != user.id))
        if not others:
            for j in db.scalars(select(Job).where(Job.company_id == m.company_id, Job.origin == "direct",
                                                  Job.status != "closed")):
                j.status, j.closed_at = "closed", utcnow()
    db.execute(delete(AlertSubscription).where(
        (AlertSubscription.user_id == user.id) | (AlertSubscription.email == user.email)))
    db.delete(user)
    db.commit()
    response.delete_cookie("sinag_session", path="/")
    return {"ok": True, "message": "Your account and files were deleted."}
