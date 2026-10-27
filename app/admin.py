"""Site administrator pages: approve employers, see what's happening."""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .auth import require_admin_user
from .config import settings
from .db import get_session
from .mailer import flush_soon, queue_email
from .models import Application, Company, CompanyMember, Job, Notification, User, aware

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin_user)])


@router.get("/overview")
def overview(db: Session = Depends(get_session)):
    users = dict(db.execute(select(User.role, func.count()).group_by(User.role)).all())
    return {
        "users": {"seekers": users.get("seeker", 0), "employers": users.get("employer", 0)},
        "pending_companies": db.scalar(select(func.count()).select_from(Company).where(Company.approval == "pending")),
        "direct_jobs_open": db.scalar(select(func.count()).select_from(Job).where(
            Job.origin == "direct", Job.status == "open")),
        "crawled_jobs_open": db.scalar(select(func.count()).select_from(Job).where(
            Job.origin == "crawled", Job.status == "open")),
        "applications": db.scalar(select(func.count()).select_from(Application)),
    }


@router.get("/companies")
def companies(approval: str = "pending", db: Session = Depends(get_session)):
    rows = db.scalars(select(Company).where(Company.source == "direct", Company.approval == approval)
                      .order_by(Company.created_at.desc()).limit(200)).all()
    out = []
    for c in rows:
        owner = db.scalar(select(User).join(CompanyMember).where(CompanyMember.company_id == c.id))
        out.append({"id": c.id, "name": c.name, "industry": c.industry, "website": c.website,
                    "location_text": c.location_text, "about": c.about, "approval": c.approval,
                    "approval_note": c.approval_note, "created_at": aware(c.created_at),
                    "owner": {"name": owner.name, "email": owner.email} if owner else None})
    return out


class DecisionIn(BaseModel):
    decision: str
    note: str = Field("", max_length=500)


@router.post("/companies/{company_id}/decision")
def decide(company_id: int, body: DecisionIn, background: BackgroundTasks, db: Session = Depends(get_session)):
    c = db.get(Company, company_id)
    if c is None or c.source != "direct":
        raise HTTPException(404, "Company not found.")
    if body.decision not in ("approved", "rejected"):
        raise HTTPException(422, "Decision must be approved or rejected.")
    if body.decision == "rejected" and not body.note.strip():
        raise HTTPException(422, "Tell the employer why, so they can fix it.")
    c.approval, c.approval_note = body.decision, body.note.strip() or None
    c.verified = body.decision == "approved"
    text = ("is verified. Your live jobs now appear on the board." if c.verified
            else f"wasn't approved yet: {c.approval_note}. Update your company details to resubmit.")
    for m in db.scalars(select(CompanyMember).where(CompanyMember.company_id == c.id)):
        db.add(Notification(user_id=m.user_id, text=f"{c.name} {text}", link="/employer.html#company"))
        queue_email(db, m.user.email, f"{c.name} on Sinag Jobs",
                    f"Hi {m.user.name},\n\n{c.name} {text}\n\n{settings.public_url}/employer.html\n")
    db.commit()
    flush_soon(background)
    return {"ok": True, "approval": c.approval}
