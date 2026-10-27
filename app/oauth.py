"""Sign in with Google, Facebook or LinkedIn (OAuth 2.0 authorization code flow).

- A button shows only for providers whose keys are in .env.
- The `state` value is stored server-side and also in a short-lived cookie, so a callback only
  works in the browser that started it. Google and LinkedIn also use PKCE.
- Matching: a known Google/Facebook/LinkedIn account signs straight in. Otherwise, if a Sinag
  account already has the same email and the provider says the email is verified, the accounts
  are linked. Otherwise a new account is created (job seeker unless they started from "employer").
- Admin accounts can't use social sign-in; they keep their password.
- In the phone app, Google and Facebook refuse sign-in inside the app's web view. The app opens the
  phone's browser instead (with app_challenge = SHA-256 of a secret only the app knows). After
  sign-in we send the browser back to the app ({APP_SCHEME}://auth?code=...), and the app swaps the
  one-time code plus its secret for a session at /api/auth/app-login. A code caught by another app
  is useless without the secret.

Redirect URIs to register with each provider:
  {PUBLIC_URL}/api/auth/oauth/google/callback
  {PUBLIC_URL}/api/auth/oauth/facebook/callback
  {PUBLIC_URL}/api/auth/oauth/linkedin/callback
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .auth import _client, current_user, make_email_token, mark_verified, rate_limit, start_session, use_email_token
from .config import settings
from .db import get_session
from datetime import timedelta

from .models import CandidateProfile, OAuthIdentity, User, aware, utcnow

log = logging.getLogger("sinag.oauth")
router = APIRouter(prefix="/api/auth", tags=["social sign-in"])
me_router = APIRouter(prefix="/api/me", tags=["social sign-in"])
STATE_COOKIE = "sinag_oauth"
_transport: httpx.BaseTransport | None = None      # tests swap in fake providers

NAMES = {"google": "Google", "facebook": "Facebook", "linkedin": "LinkedIn"}


def _cfg(provider: str) -> dict | None:
    v = settings.facebook_graph_version
    table = {
        "google": dict(id=settings.google_client_id, secret=settings.google_client_secret, pkce=True,
                       auth="https://accounts.google.com/o/oauth2/v2/auth", token="https://oauth2.googleapis.com/token",
                       userinfo="https://openidconnect.googleapis.com/v1/userinfo", scope="openid email profile"),
        "facebook": dict(id=settings.facebook_app_id, secret=settings.facebook_app_secret, pkce=False,
                         auth=f"https://www.facebook.com/{v}/dialog/oauth",
                         token=f"https://graph.facebook.com/{v}/oauth/access_token",
                         userinfo=f"https://graph.facebook.com/{v}/me?fields=id,name,email", scope="email,public_profile"),
        "linkedin": dict(id=settings.linkedin_client_id, secret=settings.linkedin_client_secret, pkce=True,
                         auth="https://www.linkedin.com/oauth/v2/authorization",
                         token="https://www.linkedin.com/oauth/v2/accessToken",
                         userinfo="https://api.linkedin.com/v2/userinfo", scope="openid profile email"),
    }
    c = table.get(provider)
    return c if c and c["id"] and c["secret"] else None


def redirect_uri(provider: str) -> str:
    return f"{settings.public_url}/api/auth/oauth/{provider}/callback"


@router.get("/providers")
def providers():
    return [{"id": p, "name": NAMES[p]} for p in NAMES if _cfg(p)]


def _safe_next(nxt: str | None) -> str | None:
    if nxt and nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt and len(nxt) < 300:
        return nxt
    return None


@router.get("/oauth/{provider}/start")
def start(provider: str, request: Request, role: str = "seeker", next: str | None = None, link: bool = False,
          app_challenge: str | None = None, db: Session = Depends(get_session)):
    c = _cfg(provider)
    if c is None:
        raise HTTPException(404, "That sign-in option isn't set up on this site.")
    rate_limit(f"oauth:{_client(request)}", limit=30, window=600)
    verifier = secrets.token_urlsafe(48)
    state = make_email_token(db, "oauth", data={"provider": provider, "role": role if role in ("seeker", "employer") else "seeker",
                                                "next": _safe_next(next), "verifier": verifier, "link": bool(link),
                                                "app_challenge": app_challenge if app_challenge and len(app_challenge) == 43 else None}, hours=1)
    db.commit()
    q = {"client_id": c["id"], "redirect_uri": redirect_uri(provider), "response_type": "code", "scope": c["scope"], "state": state}
    if c["pkce"]:
        q["code_challenge"] = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        q["code_challenge_method"] = "S256"
    if provider == "google":
        q["prompt"] = "select_account"
    resp = RedirectResponse(c["auth"] + "?" + urlencode(q), status_code=303)
    resp.set_cookie(STATE_COOKIE, state, max_age=3600, httponly=True, secure=settings.cookie_secure, samesite="lax",
                    path="/api/auth/oauth")
    return resp


def _fail(code: str) -> RedirectResponse:
    r = RedirectResponse(f"/login.html?oauth_error={code}", status_code=303)
    r.delete_cookie(STATE_COOKIE, path="/api/auth/oauth")
    return r


def _profile(provider: str, c: dict, code: str, verifier: str) -> dict:
    """Swap the code for a token, then read who signed in. Returns sub, email, email_verified, name."""
    with httpx.Client(timeout=15, transport=_transport) as h:
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(provider),
                "client_id": c["id"], "client_secret": c["secret"]}
        if c["pkce"]:
            data["code_verifier"] = verifier
        r = h.post(c["token"], data=data, headers={"Accept": "application/json"})
        if r.status_code >= 300:
            log.warning("%s token exchange failed: %s %s", provider, r.status_code, r.text[:200])
            raise ValueError("token")
        access = r.json().get("access_token")
        u = h.get(c["userinfo"], headers={"Authorization": f"Bearer {access}"})
        if u.status_code >= 300:
            raise ValueError("userinfo")
        info = u.json()
    if provider == "facebook":
        # Facebook only returns an email it has confirmed.
        return {"sub": str(info.get("id", "")), "email": info.get("email"), "email_verified": bool(info.get("email")),
                "name": info.get("name") or ""}
    return {"sub": str(info.get("sub", "")), "email": info.get("email"),
            "email_verified": info.get("email_verified") in (True, "true"), "name": info.get("name") or ""}


@router.get("/oauth/{provider}/callback")
def callback(provider: str, request: Request, code: str | None = None, state: str | None = None, error: str | None = None,
             sinag_oauth: str | None = Cookie(None), db: Session = Depends(get_session),
             sinag_session: str | None = Cookie(None)):
    c = _cfg(provider)
    if c is None:
        return _fail("not_configured")
    if error:
        return _fail("cancelled")
    if not code or not state or not sinag_oauth or not secrets.compare_digest(state, sinag_oauth):
        return _fail("expired")
    t = use_email_token(db, state, "oauth")
    if t is None or t.data.get("provider") != provider:
        return _fail("expired")
    db.commit()
    try:
        p = _profile(provider, c, code, t.data["verifier"])
    except (ValueError, httpx.HTTPError, KeyError):
        return _fail("provider")
    if not p["sub"]:
        return _fail("provider")
    email = (p["email"] or "").strip().lower()

    new_role = None
    ident = db.scalar(select(OAuthIdentity).where(OAuthIdentity.provider == provider, OAuthIdentity.subject == p["sub"]))
    user = db.get(User, ident.user_id) if ident else None

    if t.data.get("link"):                          # "Link Google" from account settings while signed in
        from .auth import optional_user
        me = optional_user(db, sinag_session)
        if me is None:
            return _fail("expired")
        if user is not None and user.id != me.id:
            return _fail("linked_elsewhere")
        if user is None:
            db.add(OAuthIdentity(user_id=me.id, provider=provider, subject=p["sub"], email=email or None))
            db.commit()
        return RedirectResponse({"seeker": "/seeker.html", "employer": "/employer.html"}.get(me.role, "/") + "?linked=" + provider,
                                status_code=303)

    if user is None:
        if not email:
            return _fail("no_email")
        user = db.scalar(select(User).where(func.lower(User.email) == email))
        if user is not None:
            if not p["email_verified"]:
                return _fail("unverified_email")
            if user.role == "admin":
                return _fail("admin")
            db.add(OAuthIdentity(user_id=user.id, provider=provider, subject=p["sub"], email=email))
        else:
            role = t.data.get("role", "seeker")
            name = (p["name"] or email.split("@")[0])[:200]
            # Consent: the sign-in page says that continuing means agreeing to the privacy notice.
            user = User(email=email, password_hash="", role=role, name=name, consent_at=utcnow())
            db.add(user)
            db.flush()
            if role == "seeker":
                db.add(CandidateProfile(user_id=user.id, full_name=name))
            db.add(OAuthIdentity(user_id=user.id, provider=provider, subject=p["sub"], email=email))
            new_role = role
        if p["email_verified"]:
            mark_verified(db, user)
    if not user.is_active:
        return _fail("disabled")
    if user.role == "admin":
        return _fail("admin")
    db.commit()
    if new_role:
        from .analytics import record
        record(db, "signup", new_role)
    dest = _safe_next(t.data.get("next")) or {"seeker": "/seeker.html", "employer": "/employer.html"}.get(user.role, "/")
    if t.data.get("app_challenge"):           # started in the phone app: hand back to the app
        code = make_email_token(db, "applogin", user.id, {"challenge": t.data["app_challenge"], "dest": dest}, hours=1)
        db.commit()
        r = RedirectResponse(f"{settings.app_scheme}://auth?code={code}", status_code=303)
        r.delete_cookie(STATE_COOKIE, path="/api/auth/oauth")
        return r
    resp = RedirectResponse(dest, status_code=303)
    start_session(db, user, resp)
    resp.delete_cookie(STATE_COOKIE, path="/api/auth/oauth")
    return resp


@router.get("/app-login")
def app_login(code: str, verifier: str, request: Request, db: Session = Depends(get_session)):
    """The phone app finishes social sign-in: one-time code + the app's secret -> a session in the app."""
    rate_limit(f"applogin:{_client(request)}", limit=20, window=600)
    t = use_email_token(db, code, "applogin")
    if t is None or aware(t.expires_at) < utcnow() + timedelta(minutes=55):   # codes live 5 minutes
        return _fail("expired")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    if not secrets.compare_digest(challenge, t.data.get("challenge", "")):
        db.commit()                      # the code is spent either way
        return _fail("expired")
    user = db.get(User, t.user_id)
    db.commit()
    if user is None or not user.is_active or user.role == "admin":
        return _fail("disabled")
    resp = RedirectResponse(_safe_next(t.data.get("dest")) or "/", status_code=303)
    start_session(db, user, resp)
    return resp


@me_router.get("/identities")
def my_identities(user: User = Depends(current_user), db: Session = Depends(get_session)):
    rows = db.scalars(select(OAuthIdentity).where(OAuthIdentity.user_id == user.id)).all()
    return {"has_password": bool(user.password_hash), "available": providers(),
            "linked": [{"id": r.id, "provider": r.provider, "name": NAMES.get(r.provider, r.provider), "email": r.email} for r in rows]}


@me_router.delete("/identities/{ident_id}")
def unlink(ident_id: int, user: User = Depends(current_user), db: Session = Depends(get_session)):
    r = db.get(OAuthIdentity, ident_id)
    if r is None or r.user_id != user.id:
        raise HTTPException(404, "Not found.")
    others = db.scalar(select(func.count()).select_from(OAuthIdentity).where(OAuthIdentity.user_id == user.id)) - 1
    if not user.password_hash and others == 0:
        raise HTTPException(400, "Set a password first (Forgot password on the sign-in page), so you can still sign in.")
    db.delete(r)
    db.commit()
    return {"ok": True}
