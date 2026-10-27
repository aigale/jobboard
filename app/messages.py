"""Direct messages between an employer's team and an applicant, one thread per application.

Rules:
- The employer writes first. The applicant can reply once the employer has written (or invited them
  to an interview), so job seekers can't cold-message companies.
- Anyone on the company's team can read and reply. Messages show who sent them.
- The other side gets an in-app notification (and phone/browser push) for every message, and one email
  per unread batch, so a quick back-and-forth doesn't flood their inbox.
"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from .auth import current_user, rate_limit, require_verified
from .config import settings
from .db import get_session
from .mailer import flush_soon, queue_email
from .models import Application, CompanyMember, Job, Message, Notification, User, aware, utcnow

router = APIRouter(prefix="/api/messages", tags=["messages"])


def _company_id(db: Session, user: User) -> int | None:
    return db.scalar(select(CompanyMember.company_id).where(CompanyMember.user_id == user.id))


def side_for(db: Session, user: User, a: Application) -> str:
    """'candidate' or 'employer' if this user may use the thread; otherwise 404."""
    if user.role == "seeker" and a.user_id == user.id:
        return "candidate"
    if user.role == "employer" and a.job.company_id == _company_id(db, user):
        return "employer"
    raise HTTPException(404, "Conversation not found.")


def load_application(db: Session, app_id: int) -> Application:
    a = db.scalar(select(Application).options(joinedload(Application.job).joinedload(Job.company), joinedload(Application.user))
                  .where(Application.id == app_id))
    if a is None:
        raise HTTPException(404, "Conversation not found.")
    return a


def candidate_name(a: Application) -> str:
    return (a.profile_snapshot or {}).get("full_name") or a.user.name or "Applicant"


def post_system(db: Session, a: Application, text: str) -> None:
    """A note in the thread from Sinag itself, e.g. 'Interview confirmed for ...'. Doesn't notify by itself."""
    db.add(Message(application_id=a.id, sender_id=None, side="system", body=text[:4000]))


def message_out(m: Message, names: dict[int, str], me_side: str) -> dict:
    return {"id": m.id, "side": m.side, "mine": m.side == me_side, "body": m.body, "at": aware(m.created_at),
            "sender": names.get(m.sender_id or 0, "Sinag Jobs" if m.side == "system" else ""),
            "read": m.read_at is not None}


def can_candidate_reply(db: Session, a: Application) -> bool:
    return bool(db.scalar(select(func.count()).select_from(Message).where(
        Message.application_id == a.id, Message.side.in_(("employer", "system")))))


@router.get("/threads")
def threads(user: User = Depends(current_user), db: Session = Depends(get_session)):
    """Every conversation with at least one message, newest first, with unread counts."""
    if user.role == "seeker":
        apps_q = select(Application.id).where(Application.user_id == user.id)
        mine = "candidate"
    elif user.role == "employer":
        cid = _company_id(db, user)
        if cid is None:
            return {"threads": [], "unread": 0}
        apps_q = select(Application.id).join(Job).where(Job.company_id == cid)
        mine = "employer"
    else:
        return {"threads": [], "unread": 0}
    last = (select(Message.application_id, func.max(Message.id).label("last_id"))
            .where(Message.application_id.in_(apps_q)).group_by(Message.application_id).subquery())
    rows = db.execute(select(Message).join(last, Message.id == last.c.last_id).order_by(Message.id.desc()).limit(200)).scalars().all()
    unread = dict(db.execute(select(Message.application_id, func.count()).where(
        Message.application_id.in_(apps_q), Message.side.notin_((mine, "system")), Message.read_at.is_(None))
        .group_by(Message.application_id)).all())
    out = []
    for m in rows:
        a = load_application(db, m.application_id)
        out.append({"application_id": a.id, "job": a.job.title, "company": a.job.company.name,
                    "candidate": candidate_name(a) if mine == "employer" else None,
                    "last": {"body": m.body[:140], "at": aware(m.created_at), "side": m.side, "mine": m.side == mine},
                    "unread": unread.get(a.id, 0)})
    return {"threads": out, "unread": sum(unread.values())}


@router.get("/{app_id}")
def thread(app_id: int, user: User = Depends(current_user), db: Session = Depends(get_session)):
    a = load_application(db, app_id)
    me = side_for(db, user, a)
    msgs = db.scalars(select(Message).where(Message.application_id == a.id).order_by(Message.id)).all()
    now = utcnow()
    for m in msgs:
        if m.side not in (me, "system") and m.read_at is None:
            m.read_at = now
    db.commit()
    ids = {m.sender_id for m in msgs if m.sender_id}
    names = {}
    for u in db.scalars(select(User).where(User.id.in_(ids or {-1}))):
        names[u.id] = candidate_name(a) if u.id == a.user_id else f"{u.name.split(' ')[0]} · {a.job.company.name}"
    return {"application_id": a.id, "job": {"id": a.job.id, "title": a.job.title}, "company": a.job.company.name,
            "candidate": candidate_name(a), "me": me,
            "can_send": a.stage != "withdrawn" and (me == "employer" or can_candidate_reply(db, a)),
            "messages": [message_out(m, names, me) for m in msgs]}


class MessageIn(BaseModel):
    body: str = Field(min_length=1, max_length=4000)

    @field_validator("body")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Write a message first.")
        return v


@router.post("/{app_id}", status_code=201)
def send(app_id: int, body: MessageIn, background: BackgroundTasks, user: User = Depends(current_user),
         db: Session = Depends(get_session)):
    a = load_application(db, app_id)
    me = side_for(db, user, a)
    require_verified(user)
    rate_limit(f"msg:{user.id}", limit=40, window=600)
    if a.stage == "withdrawn":
        raise HTTPException(400, "This application was withdrawn, so the conversation is closed.")
    if me == "candidate" and not can_candidate_reply(db, a):
        raise HTTPException(403, "You can reply here once the employer sends you a message.")
    # One email per unread batch: only email if the other side has nothing unread from us yet.
    already_unread = db.scalar(select(func.count()).select_from(Message).where(
        Message.application_id == a.id, Message.side == me, Message.read_at.is_(None)))
    m = Message(application_id=a.id, sender_id=user.id, side=me, body=body.body)
    db.add(m)
    company = a.job.company
    preview = body.body if len(body.body) <= 120 else body.body[:117] + "…"
    if me == "employer":
        db.add(Notification(user_id=a.user_id, text=f"{company.name}: {preview}", link=f"/seeker.html#messages-{a.id}"))
        if not already_unread:
            queue_email(db, a.user.email, f"New message from {company.name} about {a.job.title}",
                        f"Hi {candidate_name(a)},\n\n{company.name} sent you a message about your application for "
                        f"{a.job.title}:\n\n{body.body}\n\nReply on Sinag Jobs: {settings.public_url}/seeker.html#messages-{a.id}\n")
    else:
        for mem in db.scalars(select(CompanyMember).where(CompanyMember.company_id == company.id)):
            db.add(Notification(user_id=mem.user_id, text=f"{candidate_name(a)} ({a.job.title}): {preview}",
                                link=f"/employer.html#messages-{a.id}"))
            if not already_unread:
                queue_email(db, mem.user.email, f"{candidate_name(a)} replied about {a.job.title}",
                            f"{candidate_name(a)} wrote:\n\n{body.body}\n\n"
                            f"Reply on Sinag Jobs: {settings.public_url}/employer.html#messages-{a.id}\n")
    db.commit()
    flush_soon(background)
    return message_out(m, {user.id: candidate_name(a) if me == "candidate" else f"{user.name.split(' ')[0]} · {company.name}"}, me)
