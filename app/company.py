"""Branded company pages, company images, and team members (recruiters) per company."""
from __future__ import annotations

import re
import secrets
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from . import files
from .auth import make_email_token, require_employer, use_email_token
from .common import public_job_filter
from .config import settings
from .db import get_session
from .employer import my_company, need_company
from .mailer import flush_soon, queue_email
from .models import Company, CompanyMember, Job, Notification, User, aware, utcnow

router = APIRouter(prefix="/api/employer", tags=["company page and team"])
public_router = APIRouter(tags=["company pages"])
SIZES = ["1-10", "11-50", "51-200", "201-500", "501-1,000", "1,001-5,000", "5,000+"]
LINK_KEYS = ("facebook", "linkedin", "instagram", "tiktok", "youtube")
MAX_PHOTOS = 12


# ---------------------------------------------------------------------------
# Branding
# ---------------------------------------------------------------------------

def video_embed(url: str | None) -> str | None:
    """YouTube or Vimeo link -> privacy-friendly embed URL. Anything else is refused."""
    if not url:
        return None
    u = urlsplit(url)
    host = u.netloc.lower().removeprefix("www.").removeprefix("m.")
    vid = None
    if host == "youtu.be":
        vid = u.path.strip("/").split("/")[0]
    elif host in ("youtube.com", "youtube-nocookie.com"):
        if u.path == "/watch":
            vid = (parse_qs(u.query).get("v") or [None])[0]
        elif u.path.startswith(("/embed/", "/shorts/", "/live/")):
            vid = u.path.split("/")[2]
    if vid and re.fullmatch(r"[\w-]{6,20}", vid):
        return f"https://www.youtube-nocookie.com/embed/{vid}"
    if host == "vimeo.com":
        m = re.match(r"^/(\d+)", u.path)
        if m:
            return f"https://player.vimeo.com/video/{m.group(1)}?dnt=1"
    return None


class ValueIn(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    text: str = Field("", max_length=400)


class TestimonialIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    role: str = Field("", max_length=100)
    quote: str = Field(min_length=5, max_length=600)


class BrandingIn(BaseModel):
    tagline: str = Field("", max_length=200)
    size: str | None = None
    founded: int | None = Field(None, ge=1800, le=2100)
    video_url: str = Field("", max_length=500)
    culture_values: list[ValueIn] = Field(default_factory=list, max_length=8)
    testimonials: list[TestimonialIn] = Field(default_factory=list, max_length=8)
    links: dict[str, str] = Field(default_factory=dict)

    @field_validator("size")
    @classmethod
    def _size(cls, v):
        if v and v not in SIZES:
            raise ValueError("Choose a company size from the list.")
        return v or None

    @field_validator("video_url")
    @classmethod
    def _video(cls, v):
        v = v.strip()
        if v and not video_embed(v):
            raise ValueError("Use a YouTube or Vimeo link for the video tour.")
        return v

    @field_validator("links")
    @classmethod
    def _links(cls, v):
        out = {}
        for k, url in v.items():
            url = (url or "").strip()
            if k not in LINK_KEYS or not url:
                continue
            if not re.match(r"^https://[^\s]+$", url):
                raise ValueError(f"The {k} link must start with https://")
            out[k] = url[:300]
        return out


def branding_out(c: Company) -> dict:
    return {"tagline": c.tagline or "", "size": c.size, "founded": c.founded, "video_url": c.video_url or "",
            "video_embed": video_embed(c.video_url), "culture_values": c.culture_values or [],
            "testimonials": c.testimonials or [], "photos": c.photos or [], "links": c.links or {},
            "logo_url": c.logo_url, "cover_url": c.cover_url, "slug": c.slug, "sizes": SIZES}


@router.get("/branding")
def get_branding(company: Company = Depends(need_company)):
    return branding_out(company)


@router.put("/branding")
def put_branding(body: BrandingIn, company: Company = Depends(need_company), db: Session = Depends(get_session)):
    company.tagline, company.size, company.founded = body.tagline.strip() or None, body.size, body.founded
    company.video_url = body.video_url or None
    company.culture_values = [v.model_dump() for v in body.culture_values]
    company.testimonials = [t.model_dump() for t in body.testimonials]
    company.links = body.links
    db.commit()
    return branding_out(company)


IMAGE_EXTS = {"png", "jpg", "jpeg"}


@router.post("/branding/images", status_code=201)
async def upload_image(file: UploadFile = File(...), kind: str = Form("photo"),
                       company: Company = Depends(need_company), db: Session = Depends(get_session)):
    if kind not in ("logo", "cover", "photo"):
        raise HTTPException(422, "Choose logo, cover or photo.")
    data = await file.read(settings.max_upload_mb * 1024 * 1024 + 1)
    name = files.safe_download_name(file.filename or "image")
    ext, _ = files.validate(name, data)
    if ext not in IMAGE_EXTS:
        raise HTTPException(422, "Upload a PNG or JPG image.")
    if kind == "photo" and len(company.photos or []) >= MAX_PHOTOS:
        raise HTTPException(422, f"You can show up to {MAX_PHOTOS} photos. Remove one first.")
    folder = settings.upload_dir / "companies" / str(company.id)
    folder.mkdir(parents=True, exist_ok=True)
    stored = f"{secrets.token_hex(12)}.{ext}"
    (folder / stored).write_bytes(data)
    url = f"/media/companies/{company.id}/{stored}"
    if kind == "logo":
        _remove_media(company.logo_url)
        company.logo_url = url
    elif kind == "cover":
        _remove_media(company.cover_url)
        company.cover_url = url
    else:
        company.photos = [*(company.photos or []), url]
    db.commit()
    return branding_out(company)


class ImageRef(BaseModel):
    url: str


@router.post("/branding/images/remove")
def remove_image(body: ImageRef, company: Company = Depends(need_company), db: Session = Depends(get_session)):
    if body.url == company.logo_url:
        company.logo_url = None
    elif body.url == company.cover_url:
        company.cover_url = None
    elif body.url in (company.photos or []):
        company.photos = [p for p in company.photos if p != body.url]
    else:
        raise HTTPException(404, "Image not found.")
    _remove_media(body.url)
    db.commit()
    return branding_out(company)


def _remove_media(url: str | None) -> None:
    if url and url.startswith("/media/companies/"):
        files.remove("companies/" + url.removeprefix("/media/companies/"))


@public_router.get("/media/companies/{company_id}/{name}")
def media(company_id: int, name: str):
    if not re.fullmatch(r"[0-9a-f]{24}\.(png|jpg|jpeg)", name):
        raise HTTPException(404, "Not found.")
    p = files.path_for(f"companies/{company_id}/{name}")
    if not p.exists():
        raise HTTPException(404, "Not found.")
    return FileResponse(p, media_type="image/png" if name.endswith("png") else "image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"})


@public_router.get("/api/companies/{slug}")
def company_page(slug: str, db: Session = Depends(get_session)):
    c = db.scalar(select(Company).where(Company.slug == slug, Company.active.is_(True), Company.approval == "approved"))
    if c is None:
        raise HTTPException(404, "This company page doesn't exist.")
    jobs = db.scalars(select(Job).join(Company).options(joinedload(Job.company))
                      .where(public_job_filter(), Job.company_id == c.id).order_by(Job.posted_at.desc()).limit(100)).all()
    from .api import summary_dict
    return {"name": c.name, "industry": c.industry, "website": c.website, "location": c.location_text,
            "about": c.about, "verified": c.verified, "direct": c.source == "direct", **branding_out(c),
            "jobs": [summary_dict(j) for j in jobs]}


# ---------------------------------------------------------------------------
# Team members
# ---------------------------------------------------------------------------

def _membership(db: Session, user: User) -> CompanyMember | None:
    return db.scalar(select(CompanyMember).where(CompanyMember.user_id == user.id))


def need_owner(user: User = Depends(require_employer), db: Session = Depends(get_session)) -> CompanyMember:
    m = _membership(db, user)
    if m is None:
        raise HTTPException(409, "Set up your company profile first.")
    if m.role != "owner":
        raise HTTPException(403, "Only the company owner can manage the team.")
    return m


@router.get("/team")
def team(user: User = Depends(require_employer), company: Company = Depends(need_company), db: Session = Depends(get_session)):
    members = db.scalars(select(CompanyMember).options(joinedload(CompanyMember.user))
                         .where(CompanyMember.company_id == company.id).order_by(CompanyMember.id)).all()
    me = next((m for m in members if m.user_id == user.id), None)
    return {"my_role": me.role if me else None,
            "members": [{"id": m.id, "name": m.user.name, "email": m.user.email, "role": m.role,
                         "you": m.user_id == user.id} for m in members]}


class TeamInviteIn(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def _email(cls, v):
        from .auth import _check_email
        return _check_email(v)


@router.post("/team/invite", status_code=201)
def invite_member(body: TeamInviteIn, background: BackgroundTasks, owner: CompanyMember = Depends(need_owner),
                  db: Session = Depends(get_session)):
    company = owner.company
    count = db.scalar(select(func.count()).select_from(CompanyMember).where(CompanyMember.company_id == company.id))
    if count >= 20:
        raise HTTPException(422, "A company can have up to 20 team members.")
    existing = db.scalar(select(User).where(User.email == body.email))
    if existing and _membership(db, existing):
        raise HTTPException(409, "That person is already on a company team on Sinag Jobs.")
    if existing and existing.role != "employer":
        raise HTTPException(409, "That email belongs to a job seeker account. Ask them to use a work email.")
    token = make_email_token(db, "team", data={"company_id": company.id, "email": body.email}, hours=24 * 7)
    queue_email(db, body.email, f"Join {company.name} on Sinag Jobs",
                f"{owner.user.name} invited you to help hire for {company.name} on Sinag Jobs. You'll be able to "
                f"post jobs and review applicants.\n\nAccept the invite (works for 7 days):\n"
                f"{settings.public_url}/join.html?token={token}\n")
    db.commit()
    flush_soon(background)
    return {"ok": True, "message": f"Invite sent to {body.email}."}


@router.delete("/team/{member_id}")
def remove_member(member_id: int, owner: CompanyMember = Depends(need_owner), db: Session = Depends(get_session)):
    m = db.get(CompanyMember, member_id)
    if m is None or m.company_id != owner.company_id:
        raise HTTPException(404, "Team member not found.")
    if m.id == owner.id:
        raise HTTPException(400, "You can't remove yourself. Delete your account instead, or make someone else owner first.")
    db.delete(m)
    db.commit()
    return {"ok": True}


@router.post("/team/{member_id}/make-owner")
def make_owner(member_id: int, owner: CompanyMember = Depends(need_owner), db: Session = Depends(get_session)):
    m = db.get(CompanyMember, member_id)
    if m is None or m.company_id != owner.company_id:
        raise HTTPException(404, "Team member not found.")
    m.role, owner.role = "owner", "recruiter"
    db.commit()
    return {"ok": True}


class AcceptIn(BaseModel):
    token: str


@router.get("/team/invite-info")
def invite_info(token: str, db: Session = Depends(get_session)):
    t = use_email_token(db, token, "team", consume=False)
    if t is None:
        raise HTTPException(400, "This invite has expired or was already used. Ask for a new one.")
    c = db.get(Company, t.data["company_id"])
    return {"company": c.name if c else None, "email": t.data.get("email")}


@router.post("/team/accept")
def accept_invite(body: AcceptIn, user: User = Depends(require_employer), db: Session = Depends(get_session)):
    t = use_email_token(db, body.token, "team", consume=False)
    if t is None:
        raise HTTPException(400, "This invite has expired or was already used. Ask for a new one.")
    if user.email != t.data.get("email"):
        raise HTTPException(403, f"This invite is for {t.data.get('email')}. Sign in with that email to accept it.")
    if _membership(db, user):
        raise HTTPException(409, "Your account already belongs to a company.")
    company = db.get(Company, t.data["company_id"])
    if company is None:
        raise HTTPException(404, "That company no longer exists.")
    t.used_at = utcnow()
    db.add(CompanyMember(company_id=company.id, user_id=user.id, role="recruiter"))
    from .auth import mark_verified
    mark_verified(db, user)       # the invite link proves they read this inbox
    for m in db.scalars(select(CompanyMember).where(CompanyMember.company_id == company.id, CompanyMember.role == "owner")):
        db.add(Notification(user_id=m.user_id, text=f"{user.name} joined {company.name}", link="/employer.html#team"))
    db.commit()
    return {"ok": True, "company": company.name}
