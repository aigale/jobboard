"""Interview scheduling with calendar invites.

Flow: the employer offers 1 to 3 times -> the applicant picks one (or asks for other times) -> both
get an email with an .ics calendar invite (works with Google Calendar, Outlook and Apple Calendar)
plus an "Add to Google Calendar" link. Changes and cancellations send an updated invite with the
same UID and a higher SEQUENCE, so calendars update the existing event instead of adding a new one.
A reminder notification goes to both sides about 24 hours before.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import current_user, require_employer, require_seeker, require_verified
from .config import settings
from .db import get_session
from .mailer import flush_soon, queue_email
from .messages import candidate_name, load_application, post_system, side_for
from .models import (Application, ApplicationEvent, CompanyMember, Interview, Job, Notification, User, aware,
                     utcnow)

router = APIRouter(prefix="/api/interviews", tags=["interviews"])
employer_router = APIRouter(prefix="/api/employer", tags=["interviews"])

MODES = {"video": "Video call", "phone": "Phone call", "onsite": "In person"}
EARLY_STAGES = ("applied", "screening", "shortlisted")


def tz() -> ZoneInfo:
    return ZoneInfo(settings.timezone)


def nice(dt: datetime) -> str:
    """'Thu, Oct 30, 2026, 10:00 AM (Philippine time)'"""
    local = aware(dt).astimezone(tz())
    return local.strftime("%a, %b %d, %Y, %I:%M %p").replace(" 0", " ") + " (Philippine time)"


# ---------------------------------------------------------------------------
# Calendar files
# ---------------------------------------------------------------------------

def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n")


def _fold(line: str) -> str:
    """Lines longer than 75 bytes are split with CRLF + space (RFC 5545)."""
    out, cur = [], b""
    for ch in line:
        b = ch.encode()
        if len(cur) + len(b) > (75 if not out else 74):
            out.append(cur.decode())
            cur = b""
        cur += b
    out.append(cur.decode())
    return "\r\n ".join(out)


def _stamp(dt: datetime) -> str:
    return aware(dt).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def ics(iv: Interview, a: Application, method: str = "REQUEST") -> str:
    """method: REQUEST (new or changed) or CANCEL."""
    start = aware(iv.starts_at)
    end = start + timedelta(minutes=iv.duration_minutes)
    org_email = parseaddr(settings.smtp_from)[1] or "no-reply@localhost"
    desc = (f"{MODES.get(iv.mode, iv.mode)} interview for {a.job.title} at {a.job.company.name}.\n"
            + (f"Where: {iv.location}\n" if iv.location else "") + (f"\n{iv.notes}\n" if iv.notes else "")
            + f"\nDetails and messages: {settings.public_url}/seeker.html#interviews")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Sinag Jobs//Interviews//EN", "CALSCALE:GREGORIAN",
             f"METHOD:{method}", "BEGIN:VEVENT", f"UID:{iv.uid}", f"SEQUENCE:{iv.sequence}", f"DTSTAMP:{_stamp(utcnow())}",
             f"DTSTART:{_stamp(start)}", f"DTEND:{_stamp(end)}",
             f"SUMMARY:{_esc(('Cancelled: ' if method == 'CANCEL' else '') + f'Interview: {a.job.title} at {a.job.company.name}')}",
             f"DESCRIPTION:{_esc(desc)}", f"LOCATION:{_esc(iv.location or MODES.get(iv.mode, ''))}",
             f"ORGANIZER;CN={_esc(a.job.company.name)}:mailto:{org_email}",
             f"ATTENDEE;CN={_esc(candidate_name(a))};ROLE=REQ-PARTICIPANT;PARTSTAT=ACCEPTED:mailto:{a.user.email}",
             f"STATUS:{'CANCELLED' if method == 'CANCEL' else 'CONFIRMED'}"]
    if method != "CANCEL":
        lines += ["BEGIN:VALARM", "TRIGGER:-PT30M", "ACTION:DISPLAY", "DESCRIPTION:Interview in 30 minutes", "END:VALARM"]
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_fold(l) for l in lines) + "\r\n"


def google_link(iv: Interview, a: Application) -> str:
    start = aware(iv.starts_at)
    end = start + timedelta(minutes=iv.duration_minutes)
    q = {"action": "TEMPLATE", "text": f"Interview: {a.job.title} at {a.job.company.name}",
         "dates": f"{_stamp(start)}/{_stamp(end)}", "location": iv.location or MODES.get(iv.mode, ""),
         "details": f"{MODES.get(iv.mode, iv.mode)} interview. {iv.notes}".strip()}
    return "https://calendar.google.com/calendar/render?" + urlencode(q, quote_via=quote)


def interview_out(iv: Interview, a: Application, viewer: str) -> dict:
    out = {"id": iv.id, "application_id": a.id, "status": iv.status, "mode": iv.mode, "mode_label": MODES.get(iv.mode, iv.mode),
           "location": iv.location, "duration_minutes": iv.duration_minutes, "notes": iv.notes,
           "slots": [{"at": s, "label": nice(datetime.fromisoformat(s))} for s in iv.slots or []],
           "starts_at": aware(iv.starts_at), "when": nice(iv.starts_at) if iv.starts_at else None,
           "cancel_reason": iv.cancel_reason, "job": {"id": a.job.id, "title": a.job.title}, "company": a.job.company.name,
           "candidate": candidate_name(a) if viewer == "employer" else None, "updated_at": aware(iv.updated_at)}
    if iv.status == "confirmed" and iv.starts_at:
        out["ics_url"] = f"/api/interviews/{iv.id}.ics"
        out["google_url"] = google_link(iv, a)
    return out


# ---------------------------------------------------------------------------
# Telling people
# ---------------------------------------------------------------------------

def _team(db: Session, company_id: int) -> list[User]:
    return [m.user for m in db.scalars(select(CompanyMember).where(CompanyMember.company_id == company_id))]


def _employer_contacts(db: Session, iv: Interview, a: Application) -> list[User]:
    """Who gets the employer's copy of the invite: whoever scheduled it, else the owners."""
    if iv.created_by_id:
        u = db.get(User, iv.created_by_id)
        if u is not None and u.is_active:
            return [u]
    return [m.user for m in db.scalars(select(CompanyMember).where(CompanyMember.company_id == a.job.company_id,
                                                                   CompanyMember.role == "owner"))]


def _send_invites(db: Session, iv: Interview, a: Application, method: str, subject: str, intro: str) -> None:
    att = [{"filename": "interview.ics" if method == "REQUEST" else "cancelled.ics",
            "content_type": f"text/calendar; method={method}", "text": ics(iv, a, method)}]
    details = (f"When: {nice(iv.starts_at)}\nLength: {iv.duration_minutes} minutes\nType: {MODES.get(iv.mode, iv.mode)}\n"
               + (f"Where: {iv.location}\n" if iv.location else "") + (f"\nNotes: {iv.notes}\n" if iv.notes else ""))
    gcal = f"\nAdd to Google Calendar: {google_link(iv, a)}\n" if method == "REQUEST" else ""
    queue_email(db, a.user.email, subject, f"Hi {candidate_name(a)},\n\n{intro}\n\n{details}{gcal}\n"
                f"The attached calendar file adds it to Outlook or Apple Calendar.\n"
                f"Details and messages: {settings.public_url}/seeker.html#interviews\n", att)
    for u in _employer_contacts(db, iv, a):
        queue_email(db, u.email, f"{subject} ({candidate_name(a)})", f"{intro}\n\nCandidate: {candidate_name(a)}\n{details}{gcal}\n"
                    f"See the applicant: {settings.public_url}/employer.html#pipeline-{a.job.id}\n", att)


# ---------------------------------------------------------------------------
# Employer: propose, reschedule, list
# ---------------------------------------------------------------------------

def check_slots(v: list[datetime]) -> list[datetime]:
    now = utcnow()
    out = []
    for s in v:
        if s.tzinfo is None:                      # a time without a zone is Philippine time
            s = s.replace(tzinfo=ZoneInfo(settings.timezone))
        if s <= now + timedelta(minutes=30):
            raise ValueError("Pick times at least 30 minutes from now.")
        if s > now + timedelta(days=120):
            raise ValueError("Pick times within the next 4 months.")
        out.append(s.astimezone(timezone.utc))
    if len(set(out)) != len(out):
        raise ValueError("Each time should be different.")
    return sorted(out)


class InterviewIn(BaseModel):
    mode: str = Field("video", pattern="^(video|phone|onsite)$")
    location: str = Field("", max_length=500)
    duration_minutes: int = Field(30, ge=10, le=480)
    notes: str = Field("", max_length=2000)
    slots: list[datetime] = Field(min_length=1, max_length=3)

    @field_validator("slots")
    @classmethod
    def _future(cls, v: list[datetime]) -> list[datetime]:
        return check_slots(v)

    @field_validator("location")
    @classmethod
    def _loc(cls, v: str) -> str:
        v = v.strip()
        if v.lower().startswith(("javascript:", "data:")):
            raise ValueError("Enter a meeting link that starts with https://, a phone note, or an address.")
        return v


def _own_app(db: Session, user: User, app_id: int) -> Application:
    a = load_application(db, app_id)
    side_for(db, user, a)          # 404 unless it's this company's applicant
    return a


@employer_router.post("/applications/{app_id}/interviews", status_code=201)
def propose(app_id: int, body: InterviewIn, background: BackgroundTasks, user: User = Depends(require_employer),
            db: Session = Depends(get_session)):
    require_verified(user)
    a = _own_app(db, user, app_id)
    if a.stage in ("withdrawn", "rejected", "hired"):
        raise HTTPException(400, "You can't schedule an interview for this application.")
    if db.scalar(select(Interview.id).where(Interview.application_id == a.id, Interview.status.in_(("proposed", "confirmed")))):
        raise HTTPException(409, "There's already an interview for this applicant. Change or cancel that one first.")
    iv = Interview(application_id=a.id, created_by_id=user.id, mode=body.mode, location=body.location,
                   duration_minutes=body.duration_minutes, notes=body.notes.strip(),
                   slots=[s.isoformat() for s in body.slots], uid=f"{secrets.token_hex(12)}@sinag-jobs", sequence=0)
    db.add(iv)
    if a.stage in EARLY_STAGES:
        db.add(ApplicationEvent(application_id=a.id, kind="stage", from_stage=a.stage, to_stage="interview",
                                actor_id=user.id, visible_to_candidate=True))
        a.stage = "interview"
        a.viewed_at = a.viewed_at or utcnow()
    _ask_candidate(db, iv, a, first=True)
    db.commit()
    flush_soon(background)
    return interview_out(iv, a, "employer")


def _ask_candidate(db: Session, iv: Interview, a: Application, first: bool) -> None:
    company = a.job.company
    times = "\n".join(f"- {nice(datetime.fromisoformat(s))}" for s in iv.slots)
    what = "invited you to an interview" if first else "suggested new interview times"
    post_system(db, a, f"{company.name} {what}. Choose a time under Interviews.")
    db.add(Notification(user_id=a.user_id, text=f"{company.name} {what} for {a.job.title}. Pick a time.",
                        link="/seeker.html#interviews"))
    queue_email(db, a.user.email, f"Interview invitation: {a.job.title} at {company.name}",
                f"Hi {candidate_name(a)},\n\n{company.name} {what} for {a.job.title}. Choose the time that works for you:\n\n"
                f"{times}\n\n{MODES.get(iv.mode, iv.mode)}, about {iv.duration_minutes} minutes."
                + (f"\n\n{iv.notes}" if iv.notes else "")
                + f"\n\nPick a time: {settings.public_url}/seeker.html#interviews\n"
                  "If none of these work, you can ask for other times on the same page.\n")


class RescheduleIn(BaseModel):
    slots: list[datetime] = Field(min_length=1, max_length=3)
    note: str = Field("", max_length=500)

    @field_validator("slots")
    @classmethod
    def _future(cls, v: list[datetime]) -> list[datetime]:
        return check_slots(v)


@employer_router.post("/interviews/{iv_id}/reschedule")
def reschedule(iv_id: int, body: RescheduleIn, background: BackgroundTasks, user: User = Depends(require_employer),
               db: Session = Depends(get_session)):
    iv, a = _load(db, iv_id)
    side_for(db, user, a)
    if iv.status == "cancelled":
        raise HTTPException(400, "This interview was cancelled. Schedule a new one.")
    was_confirmed = iv.status == "confirmed"
    iv.slots, iv.status, iv.starts_at, iv.reminder_sent_at = [s.isoformat() for s in body.slots], "proposed", None, None
    iv.sequence += 1
    if body.note.strip():
        iv.notes = (iv.notes + "\n\n" if iv.notes else "") + body.note.strip()
    if was_confirmed:
        post_system(db, a, "The confirmed interview time was changed. Pick a new time.")
    _ask_candidate(db, iv, a, first=False)
    db.commit()
    flush_soon(background)
    return interview_out(iv, a, "employer")


@employer_router.get("/interviews")
def company_interviews(user: User = Depends(require_employer), db: Session = Depends(get_session)):
    cid = db.scalar(select(CompanyMember.company_id).where(CompanyMember.user_id == user.id))
    rows = db.scalars(select(Interview).join(Application).join(Job).where(Job.company_id == cid)
                      .order_by(Interview.starts_at.is_(None), Interview.starts_at, Interview.id.desc()).limit(200)).all()
    out = []
    for iv in rows:
        a = load_application(db, iv.application_id)
        out.append(interview_out(iv, a, "employer"))
    return out


# ---------------------------------------------------------------------------
# Both sides
# ---------------------------------------------------------------------------

def _load(db: Session, iv_id: int) -> tuple[Interview, Application]:
    iv = db.get(Interview, iv_id)
    if iv is None:
        raise HTTPException(404, "Interview not found.")
    return iv, load_application(db, iv.application_id)


@router.get("/mine")
def my_interviews(user: User = Depends(require_seeker), db: Session = Depends(get_session)):
    rows = db.scalars(select(Interview).join(Application).where(Application.user_id == user.id)
                      .order_by(Interview.id.desc()).limit(100)).all()
    return [interview_out(iv, load_application(db, iv.application_id), "candidate") for iv in rows]


@router.get("/for-application/{app_id}")
def for_application(app_id: int, user: User = Depends(current_user), db: Session = Depends(get_session)):
    a = load_application(db, app_id)
    viewer = side_for(db, user, a)
    rows = db.scalars(select(Interview).where(Interview.application_id == a.id).order_by(Interview.id.desc())).all()
    return [interview_out(iv, a, viewer) for iv in rows]


class AcceptIn(BaseModel):
    slot: datetime


@router.post("/{iv_id}/accept")
def accept(iv_id: int, body: AcceptIn, background: BackgroundTasks, user: User = Depends(require_seeker),
           db: Session = Depends(get_session)):
    iv, a = _load(db, iv_id)
    if side_for(db, user, a) != "candidate":
        raise HTTPException(404, "Interview not found.")
    if iv.status != "proposed":
        raise HTTPException(400, "This interview isn't waiting for a time.")
    chosen = body.slot if body.slot.tzinfo else body.slot.replace(tzinfo=tz())
    match = next((s for s in iv.slots if datetime.fromisoformat(s) == chosen), None)
    if match is None:
        raise HTTPException(422, "Pick one of the offered times.")
    if datetime.fromisoformat(match) <= utcnow():
        raise HTTPException(400, "That time has passed. Ask the employer for new times.")
    iv.starts_at, iv.status = datetime.fromisoformat(match), "confirmed"
    iv.sequence += 1
    post_system(db, a, f"Interview confirmed: {nice(iv.starts_at)}.")
    for u in _team(db, a.job.company_id):
        db.add(Notification(user_id=u.id, text=f"{candidate_name(a)} confirmed an interview for {nice(iv.starts_at)}.",
                            link=f"/employer.html#interviews"))
    _send_invites(db, iv, a, "REQUEST", f"Interview confirmed: {a.job.title} at {a.job.company.name}",
                  f"Your interview for {a.job.title} at {a.job.company.name} is confirmed.")
    db.commit()
    flush_soon(background)
    return interview_out(iv, a, "candidate")


class ReasonIn(BaseModel):
    reason: str = Field("", max_length=500)


@router.post("/{iv_id}/decline")
def ask_other_times(iv_id: int, body: ReasonIn, background: BackgroundTasks, user: User = Depends(require_seeker),
                    db: Session = Depends(get_session)):
    """None of the offered times work. The interview stays open; the employer is asked for new times."""
    iv, a = _load(db, iv_id)
    if side_for(db, user, a) != "candidate":
        raise HTTPException(404, "Interview not found.")
    if iv.status != "proposed":
        raise HTTPException(400, "This interview isn't waiting for a time.")
    iv.status = "declined"
    msg = f"{candidate_name(a)} asked for other interview times" + (f": \"{body.reason.strip()}\"" if body.reason.strip() else ".")
    post_system(db, a, msg)
    for u in _team(db, a.job.company_id):
        db.add(Notification(user_id=u.id, text=msg[:500], link=f"/employer.html#interviews"))
    for u in _employer_contacts(db, iv, a):
        queue_email(db, u.email, f"{candidate_name(a)} needs other interview times",
                    f"{msg}\n\nSuggest new times: {settings.public_url}/employer.html#interviews\n")
    db.commit()
    flush_soon(background)
    return interview_out(iv, a, "candidate")


@router.post("/{iv_id}/cancel")
def cancel(iv_id: int, body: ReasonIn, background: BackgroundTasks, user: User = Depends(current_user),
           db: Session = Depends(get_session)):
    iv, a = _load(db, iv_id)
    who = side_for(db, user, a)
    if iv.status == "cancelled":
        return interview_out(iv, a, who)
    was_confirmed = iv.status == "confirmed" and iv.starts_at is not None
    iv.status, iv.cancel_reason = "cancelled", body.reason.strip() or None
    iv.sequence += 1
    by = a.job.company.name if who == "employer" else candidate_name(a)
    note = f"{by} cancelled the interview" + (f": \"{iv.cancel_reason}\"" if iv.cancel_reason else ".")
    post_system(db, a, note)
    if who == "employer":
        db.add(Notification(user_id=a.user_id, text=note[:500], link="/seeker.html#interviews"))
    else:
        for u in _team(db, a.job.company_id):
            db.add(Notification(user_id=u.id, text=note[:500], link="/employer.html#interviews"))
    if was_confirmed:
        _send_invites(db, iv, a, "CANCEL", f"Cancelled: interview for {a.job.title}", note)
    elif who == "employer":
        queue_email(db, a.user.email, f"Interview cancelled: {a.job.title}", f"Hi {candidate_name(a)},\n\n{note}\n")
    db.commit()
    flush_soon(background)
    return interview_out(iv, a, who)


@router.get("/{iv_id}.ics")
def download_ics(iv_id: int, user: User = Depends(current_user), db: Session = Depends(get_session)):
    iv, a = _load(db, iv_id)
    side_for(db, user, a)
    if iv.starts_at is None or iv.status not in ("confirmed", "cancelled"):
        raise HTTPException(400, "The interview time isn't confirmed yet.")
    method = "CANCEL" if iv.status == "cancelled" else "REQUEST"
    return Response(ics(iv, a, method), media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="interview-{iv.id}.ics"'})


# ---------------------------------------------------------------------------
# Worker: reminders a day before
# ---------------------------------------------------------------------------

def send_reminders(session_factory, now: datetime | None = None) -> int:
    now = now or utcnow()
    n = 0
    with session_factory() as db:
        due = db.scalars(select(Interview).where(Interview.status == "confirmed", Interview.reminder_sent_at.is_(None),
                                                 Interview.starts_at > now, Interview.starts_at <= now + timedelta(hours=24))).all()
        for iv in due:
            a = load_application(db, iv.application_id)
            iv.reminder_sent_at = now
            text = f"Reminder: interview for {a.job.title} at {a.job.company.name}, {nice(iv.starts_at)}."
            db.add(Notification(user_id=a.user_id, text=text, link="/seeker.html#interviews"))
            for u in _employer_contacts(db, iv, a):
                db.add(Notification(user_id=u.id, text=f"Reminder: interview with {candidate_name(a)}, {nice(iv.starts_at)}.",
                                    link="/employer.html#interviews"))
            n += 1
        db.commit()
    return n
