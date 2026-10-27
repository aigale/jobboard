"""Accounts: sign up, sign in, sessions, password reset, and the permission checks other modules use."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Cookie, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, field_validator
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_session
from .mailer import flush_soon, queue_email
from .models import (AlertSubscription, CandidateProfile, CompanyMember, EmailToken, PasswordReset, User,
                     UserSession, aware, utcnow)

COOKIE = "sinag_session"
router = APIRouter(prefix="/api/auth", tags=["accounts"])


# ---------------------------------------------------------------------------
# Passwords and tokens
# ---------------------------------------------------------------------------

_N, _R, _P = 2 ** 14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split("$")
        test = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                              dklen=len(bytes.fromhex(digest)))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(test.hex(), digest)


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _check_password(pw: str) -> str:
    if len(pw) < 8:
        raise ValueError("Use at least 8 characters for your password.")
    if len(pw) > 200:
        raise ValueError("That password is too long.")
    return pw


def _check_email(v: str) -> str:
    import re
    v = v.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v) or len(v) > 320:
        raise ValueError("Enter a valid email address, like juan@example.com")
    return v


# ---------------------------------------------------------------------------
# Simple in-memory rate limit for sign-in and reset requests
# ---------------------------------------------------------------------------

_attempts: dict[str, deque] = defaultdict(deque)


def rate_limit(key: str, limit: int = 10, window: int = 900) -> None:
    now = time.monotonic()
    q = _attempts[key]
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(429, "Too many attempts. Wait 15 minutes and try again.")
    q.append(now)


def _client(request: Request) -> str:
    # The last X-Forwarded-For entry is the one our own proxy (Caddy) added; earlier ones can be faked.
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[-1].strip() or (request.client.host if request.client else "?")


# ---------------------------------------------------------------------------
# Sessions and permission dependencies
# ---------------------------------------------------------------------------

def start_session(db: Session, user: User, response: Response) -> None:
    token = secrets.token_urlsafe(32)
    db.add(UserSession(token_hash=_sha(token), user_id=user.id,
                       expires_at=utcnow() + timedelta(days=settings.session_days)))
    user.last_login_at = utcnow()
    db.commit()
    response.set_cookie(COOKIE, token, max_age=settings.session_days * 86400, httponly=True,
                        secure=settings.cookie_secure, samesite="lax", path="/")


def optional_user(db: Session = Depends(get_session), sinag_session: str | None = Cookie(None)) -> User | None:
    if not sinag_session:
        return None
    s = db.scalar(select(UserSession).where(UserSession.token_hash == _sha(sinag_session)))
    if s is None or aware(s.expires_at) < utcnow() or not s.user.is_active:
        return None
    return s.user


def current_user(user: User | None = Depends(optional_user)) -> User:
    if user is None:
        raise HTTPException(401, "Sign in to continue.")
    return user


def require_seeker(user: User = Depends(current_user)) -> User:
    if user.role != "seeker":
        raise HTTPException(403, "This is for job seeker accounts.")
    return user


def require_employer(user: User = Depends(current_user)) -> User:
    if user.role != "employer":
        raise HTTPException(403, "This is for employer accounts.")
    return user


def require_verified(user: User) -> None:
    if settings.require_email_verification and user.email_verified_at is None:
        raise HTTPException(403, "Confirm your email address first. We sent you a link; you can ask for a new one at the top of your dashboard.")


# ---------------------------------------------------------------------------
# One-time email links
# ---------------------------------------------------------------------------

def make_email_token(db: Session, purpose: str, user_id: int | None = None, data: dict | None = None,
                     hours: int = 72) -> str:
    token = secrets.token_urlsafe(32)
    db.add(EmailToken(token_hash=_sha(token), purpose=purpose, user_id=user_id, data=data or {},
                      expires_at=utcnow() + timedelta(hours=hours)))
    return token


def use_email_token(db: Session, token: str, purpose: str, consume: bool = True) -> EmailToken | None:
    t = db.scalar(select(EmailToken).where(EmailToken.token_hash == _sha(token or ""), EmailToken.purpose == purpose))
    if t is None or t.used_at is not None or aware(t.expires_at) < utcnow():
        return None
    if consume:
        t.used_at = utcnow()
    return t


def send_verification(db: Session, user: User) -> None:
    token = make_email_token(db, "verify", user.id, hours=72)
    queue_email(db, user.email, "Confirm your email for Sinag Jobs",
                f"Hi {user.name},\n\nConfirm your email address to finish setting up your Sinag Jobs account:\n\n"
                f"{settings.public_url}/api/auth/verify?token={token}\n\n"
                "The link works for 3 days. If you didn't sign up, you can ignore this email.\n")


def mark_verified(db: Session, user: User) -> None:
    if user.email_verified_at is None:
        user.email_verified_at = utcnow()
    for a in db.scalars(select(AlertSubscription).where(AlertSubscription.user_id == user.id)):
        a.confirmed = True


def require_admin_user(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "This is for site administrators.")
    return user


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

class SignupIn(BaseModel):
    email: str
    password: str
    name: str
    role: str
    consent: bool = False

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return _check_email(v)

    @field_validator("password")
    @classmethod
    def _pw(cls, v: str) -> str:
        return _check_password(v)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = " ".join(v.split())
        if not 2 <= len(v) <= 200:
            raise ValueError("Enter your name.")
        return v

    @field_validator("role")
    @classmethod
    def _role(cls, v: str) -> str:
        if v not in ("seeker", "employer"):
            raise ValueError("Choose job seeker or employer.")
        return v


class LoginIn(BaseModel):
    email: str
    password: str


class ForgotIn(BaseModel):
    email: str


class ResetIn(BaseModel):
    token: str
    password: str

    @field_validator("password")
    @classmethod
    def _pw(cls, v: str) -> str:
        return _check_password(v)


class ChangePasswordIn(BaseModel):
    current_password: str = ""      # not needed when the account has no password yet (made with social sign-in)
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _pw(cls, v: str) -> str:
        return _check_password(v)


def user_out(db: Session, user: User) -> dict:
    out = {"id": user.id, "email": user.email, "name": user.name, "role": user.role, "lang": user.lang or "en",
           "has_password": bool(user.password_hash),
           "email_verified": user.email_verified_at is not None or not settings.require_email_verification}
    if user.role == "employer":
        m = db.scalar(select(CompanyMember).where(CompanyMember.user_id == user.id))
        out["company"] = ({"id": m.company.id, "name": m.company.name, "approval": m.company.approval}
                          if m else None)
    return out


@router.post("/signup", status_code=201)
def signup(body: SignupIn, request: Request, response: Response, background: BackgroundTasks,
           db: Session = Depends(get_session)):
    rate_limit(f"signup:{_client(request)}", limit=20, window=3600)
    if not body.consent:
        raise HTTPException(422, "Please agree to the privacy notice to create an account.")
    if db.scalar(select(User.id).where(User.email == body.email)):
        raise HTTPException(409, "An account with this email already exists. Sign in instead.")
    user = User(email=body.email, password_hash=hash_password(body.password), role=body.role,
                name=body.name, consent_at=utcnow())
    db.add(user)
    db.flush()
    if body.role == "seeker":
        db.add(CandidateProfile(user_id=user.id, full_name=body.name))
    send_verification(db, user)
    db.commit()
    flush_soon(background)
    from .analytics import record
    record(db, "signup", body.role)
    start_session(db, user, response)
    return user_out(db, user)


@router.get("/verify")
def verify(token: str, db: Session = Depends(get_session)):
    t = use_email_token(db, token, "verify")
    if t is None:
        return RedirectResponse("/login.html?verify=expired", status_code=303)
    user = db.get(User, t.user_id)
    mark_verified(db, user)
    db.commit()
    return RedirectResponse({"seeker": "/seeker.html", "employer": "/employer.html"}.get(user.role, "/") + "?verified=1",
                            status_code=303)


@router.post("/resend-verification")
def resend_verification(request: Request, background: BackgroundTasks, user: User = Depends(current_user),
                        db: Session = Depends(get_session)):
    if user.email_verified_at is not None:
        return {"ok": True, "message": "Your email is already confirmed."}
    rate_limit(f"verify:{user.id}", limit=5, window=3600)
    send_verification(db, user)
    db.commit()
    flush_soon(background)
    return {"ok": True, "message": f"We sent a new link to {user.email}. Check your inbox and spam folder."}


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_session)):
    email = body.email.strip().lower()
    rate_limit(f"login:{_client(request)}:{email}")
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not user.is_active or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "That email and password don't match. Check them, or reset your password.")
    start_session(db, user, response)
    return user_out(db, user)


@router.post("/logout")
def logout(response: Response, db: Session = Depends(get_session), sinag_session: str | None = Cookie(None)):
    if sinag_session:
        db.execute(delete(UserSession).where(UserSession.token_hash == _sha(sinag_session)))
        db.commit()
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(user: User | None = Depends(optional_user), db: Session = Depends(get_session)):
    return {"user": user_out(db, user) if user else None}


@router.post("/forgot")
def forgot(body: ForgotIn, request: Request, background: BackgroundTasks, db: Session = Depends(get_session)):
    rate_limit(f"forgot:{_client(request)}", limit=5, window=900)
    email = body.email.strip().lower()
    user = db.scalar(select(User).where(User.email == email, User.is_active.is_(True)))
    if user:
        token = secrets.token_urlsafe(32)
        db.add(PasswordReset(token_hash=_sha(token), user_id=user.id, expires_at=utcnow() + timedelta(hours=1)))
        queue_email(db, user.email, "Reset your Sinag Jobs password",
                    f"Hi {user.name},\n\nSomeone asked to reset the password for your Sinag Jobs account.\n"
                    f"Open this link within 1 hour to choose a new password:\n\n"
                    f"{settings.public_url}/reset.html?token={token}\n\n"
                    "If this wasn't you, you can ignore this email; your password stays the same.\n")
        db.commit()
        flush_soon(background)
    # Same answer either way, so nobody can test which emails have accounts.
    return {"ok": True, "message": "If that email has an account, a reset link is on its way. Check your inbox."}


@router.post("/reset")
def reset(body: ResetIn, response: Response, db: Session = Depends(get_session)):
    pr = db.scalar(select(PasswordReset).where(PasswordReset.token_hash == _sha(body.token)))
    if pr is None or pr.used_at is not None or aware(pr.expires_at) < utcnow():
        raise HTTPException(400, "This reset link has expired or was already used. Ask for a new one.")
    user = db.get(User, pr.user_id)
    user.password_hash = hash_password(body.password)
    pr.used_at = utcnow()
    mark_verified(db, user)          # they proved they read this inbox
    db.execute(delete(UserSession).where(UserSession.user_id == user.id))   # sign out everywhere
    db.commit()
    start_session(db, user, response)
    return user_out(db, user)


@router.post("/change-password")
def change_password(body: ChangePasswordIn, user: User = Depends(current_user), db: Session = Depends(get_session)):
    had = bool(user.password_hash)
    if had and not verify_password(body.current_password, user.password_hash):
        raise HTTPException(400, "Your current password isn't right.")
    user.password_hash = hash_password(body.new_password)
    db.commit()
    return {"ok": True, "message": "Password changed." if had else "Password set. You can now also sign in with your email."}


def cleanup_sessions(db: Session) -> None:
    now = datetime.now(timezone.utc)
    db.execute(delete(UserSession).where(UserSession.expires_at < now))
    db.execute(delete(PasswordReset).where(PasswordReset.expires_at < now - timedelta(days=1)))
    db.execute(delete(EmailToken).where(EmailToken.expires_at < now - timedelta(days=1)))
    db.commit()
