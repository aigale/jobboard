"""Billing: job posts, featured listings, subscription plans and job seeker extras, paid with TEST payments only.

Two payment modes, both test-only:
- "sandbox" (default): a built-in payment simulator. It accepts only published test card numbers and
  simulated GCash/Maya payments, refuses anything that looks like a real card, and never stores card numbers.
- "paymongo_test": PayMongo's hosted checkout in test mode. Billing switches itself off if the secret
  key isn't a test key (sk_test_...), and webhooks from live mode are ignored.

Every order is marked test=True and every receipt says no real charges were made. Invoices are
printable but marked TEST: they are not BIR-registered invoices or official receipts.

Discount codes take a percentage or a fixed amount off one order (the first payment of a plan,
not its renewals). A code that brings the price to zero completes the order without any payment.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import current_user, require_admin_user, require_employer, require_seeker
from .config import settings
from .db import get_session
from .mailer import flush_soon, queue_email
from .models import (CandidateProfile, Company, CompanyMember, CreditEntry, DiscountCode, Job, Notification, Order,
                     Product, Subscription, User, aware, utcnow)

log = logging.getLogger("sinag.billing")
router = APIRouter(prefix="/api/billing", tags=["billing (test payments only)"])
admin_router = APIRouter(prefix="/api/admin/billing", tags=["admin billing"], dependencies=[Depends(require_admin_user)])

TEST_NOTICE = "This is a test environment. Do not enter real payment details."
RECEIPT_NOTICE = "TEST RECEIPT: no real charges were made."
INVOICE_NOTICE = "TEST INVOICE. Not a BIR-registered invoice or official receipt. No real charges were made."
SEEKER_KINDS = ("seeker_highlight", "seeker_priority")
JOB_POST_DAYS = 30
GRACE_DAYS = 3

# Published test numbers only. Nothing else is accepted.
TEST_CARDS = {
    "4242424242424242": {"brand": "Visa", "outcome": "success", "label": "Payment succeeds"},
    "5555555555554444": {"brand": "Mastercard", "outcome": "success", "label": "Payment succeeds"},
    "4000000000000002": {"brand": "Visa", "outcome": "declined", "label": "Card is declined"},
    "4000000000009995": {"brand": "Visa", "outcome": "insufficient_funds", "label": "Insufficient funds"},
}
FAILURES = {"declined": "The test card was declined.", "insufficient_funds": "The test card has insufficient funds."}

DEFAULT_PRODUCTS = [
    dict(code="job_post", name="Single job post", kind="job_credit", price_php=1499, credits=1, sort=1,
         description="One job post, live for up to 30 days."),
    dict(code="job_pack_5", name="5 job posts", kind="job_credit", price_php=5999, credits=5, sort=2,
         description="Five job posts to use any time. Each stays live for up to 30 days."),
    dict(code="featured_7", name="Featured for 7 days", kind="featured", price_php=499, days=7, sort=3,
         description="Pin one job to the top of search results with a highlight for 7 days."),
    dict(code="starter", name="Starter plan", kind="subscription", price_php=2999, days=30, sort=4,
         description="Up to 5 live jobs, resume database, 2 featured boosts a month.",
         limits={"live_jobs": 5, "featured_per_period": 2, "resume_db": True, "bulk_import": False}),
    dict(code="pro", name="Pro plan", kind="subscription", price_php=7999, days=30, sort=5,
         description="Unlimited live jobs, resume database, bulk import, 10 featured boosts a month.",
         limits={"live_jobs": None, "featured_per_period": 10, "resume_db": True, "bulk_import": True}),
    # Job seeker extras
    dict(code="seeker_highlight_30", name="Highlighted profile, 30 days", kind="seeker_highlight", price_php=299, days=30,
         sort=10, description="Show near the top of employers' candidate searches with a Highlighted badge for 30 days. "
                              "Your profile must be visible to employers."),
    dict(code="seeker_priority_5", name="5 priority applications", kind="seeker_priority", price_php=149, credits=5,
         sort=11, description="Mark an application as Priority: employers see it at the top of their applicant list."),
]

_transport: httpx.BaseTransport | None = None      # tests swap in a fake PayMongo


# ---------------------------------------------------------------------------
# Safety checks
# ---------------------------------------------------------------------------

def config_problem() -> str | None:
    """Why billing can't run right now, or None. Live payment keys are always refused."""
    if settings.payment_mode == "sandbox":
        return None
    if settings.payment_mode != "paymongo_test":
        return f"PAYMENT_MODE must be sandbox or paymongo_test, not {settings.payment_mode!r}."
    key = settings.paymongo_secret_key
    if not key:
        return "Add a PayMongo test secret key (PAYMONGO_SECRET_KEY=sk_test_...) or set PAYMENT_MODE=sandbox."
    if not key.startswith("sk_test_"):
        return ("Payments are switched off: this site only accepts PayMongo TEST keys (sk_test_...). "
                "Live keys are refused so no real money can be charged.")
    return None


def _need_billing() -> None:
    if not settings.billing_enabled:
        raise HTTPException(404, "Billing is turned off on this site.")
    problem = config_problem()
    if problem:
        raise HTTPException(503, problem)


def luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for d in reversed(digits):
        n = int(d)
        if alt:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
        alt = not alt
    return total % 10 == 0


# ---------------------------------------------------------------------------
# Products and entitlements
# ---------------------------------------------------------------------------

def ensure_products(db: Session) -> None:
    """Add any default product that isn't in the database yet (admins' price edits are kept)."""
    have = set(db.scalars(select(Product.code)))
    missing = [p for p in DEFAULT_PRODUCTS if p["code"] not in have]
    if not missing:
        return
    for p in missing:
        db.add(Product(**{"limits": {}, "credits": 0, "days": 0, **p}))
    try:
        db.commit()
    except IntegrityError:
        # A page can ask for billing summary in parallel with another request on
        # a fresh database. Both may read the same empty table and race to seed
        # it. The winner's products are already there; keep the request alive
        # instead of returning a 500 for a duplicate primary key.
        db.rollback()


def audience_of(p: Product) -> str:
    return "seeker" if p.kind in SEEKER_KINDS else "employer"


def product_out(p: Product) -> dict:
    return {"code": p.code, "name": p.name, "kind": p.kind, "description": p.description, "price_php": p.price_php,
            "credits": p.credits, "days": p.days, "limits": p.limits or {}, "active": p.active, "audience": audience_of(p)}


def active_subscription(db: Session, company_id: int, now: datetime | None = None) -> Subscription | None:
    now = now or utcnow()
    sub = db.scalar(select(Subscription).where(Subscription.company_id == company_id,
                                               Subscription.status.in_(("active", "past_due")))
                    .order_by(Subscription.id.desc()))
    if sub is None:
        return None
    if sub.status == "past_due" and aware(sub.current_period_end) + timedelta(days=GRACE_DAYS) < now:
        return None
    return sub


def credit_balance(db: Session, company_id: int) -> int:
    return db.scalar(select(func.coalesce(func.sum(CreditEntry.delta), 0)).where(CreditEntry.company_id == company_id)) or 0


def live_jobs(db: Session, company_id: int, now: datetime | None = None, exclude_id: int | None = None) -> int:
    now = now or utcnow()
    q = select(func.count()).select_from(Job).where(
        Job.company_id == company_id, Job.origin == "direct", Job.status == "open", Job.paid_via.is_not(None),
        (Job.expires_at.is_(None)) | (Job.expires_at > now))
    if exclude_id:
        q = q.where(Job.id != exclude_id)
    return db.scalar(q) or 0


def _plan_boosts_used(db: Session, company_id: int, since: datetime) -> int:
    return db.scalar(select(func.count()).select_from(CreditEntry).where(
        CreditEntry.company_id == company_id, CreditEntry.reason == "plan featured boost", CreditEntry.at >= since)) or 0


def entitlements(db: Session, company: Company) -> dict:
    ensure_products(db)
    sub = active_subscription(db, company.id)
    plan = db.get(Product, sub.product_code) if sub else None
    limits = (plan.limits or {}) if plan else {}
    boosts_left = 0
    if sub and limits.get("featured_per_period"):
        boosts_left = max(0, limits["featured_per_period"] - _plan_boosts_used(db, company.id, aware(sub.current_period_start)))
    unlimited = not settings.billing_enabled
    return {
        "billing_enabled": settings.billing_enabled,
        "plan": {"code": plan.code, "name": plan.name} if plan else None,
        "subscription": None if sub is None else {
            "status": sub.status, "current_period_end": aware(sub.current_period_end),
            "cancel_at_period_end": sub.cancel_at_period_end, "card": f"{sub.renew_brand} ending {sub.renew_last4}" if sub.renew_last4 else None},
        "credits": credit_balance(db, company.id),
        "live_jobs": live_jobs(db, company.id),
        "live_limit": None if (unlimited or (plan and limits.get("live_jobs") is None)) else (limits.get("live_jobs") if plan else None),
        "free_live_jobs": settings.free_live_jobs,
        "featured_boosts_left": boosts_left,
        "resume_db": unlimited or bool(limits.get("resume_db")),
        "bulk_import": unlimited or bool(limits.get("bulk_import")),
    }


class PaymentRequired(HTTPException):
    def __init__(self, msg: str):
        super().__init__(402, msg)


def charge_publish(db: Session, company: Company, job: Job) -> None:
    """Called when a job goes live. Uses the plan, the free allowance, or a job credit, in that order."""
    if job.paid_via or job.status != "open":
        return
    if not settings.billing_enabled:
        job.paid_via = "free"
        return
    now = utcnow()
    others = live_jobs(db, company.id, now, exclude_id=job.id)
    sub = active_subscription(db, company.id, now)
    if sub is not None and sub.status == "active":
        plan = db.get(Product, sub.product_code)
        limit = (plan.limits or {}).get("live_jobs") if plan else 0
        if limit is None or others < limit:
            job.paid_via = "subscription"
            return
    if others < settings.free_live_jobs:
        job.paid_via = "free"
    elif credit_balance(db, company.id) > 0:
        job.paid_via = "credit"
        db.add(CreditEntry(company_id=company.id, delta=-1, reason="job post", job_id=job.id))
    else:
        raise PaymentRequired("You've used your free job post. Buy a job post or a plan under Billing to publish more. "
                              "Your job is saved as a draft until then.")
    start = aware(job.publish_at) if job.publish_at and aware(job.publish_at) > now else now
    cap = start + timedelta(days=JOB_POST_DAYS)
    if job.expires_at is None or aware(job.expires_at) > cap:
        job.expires_at = cap


def require_feature(db: Session, company: Company, feature: str, what: str) -> None:
    if not settings.billing_enabled:
        return
    if not entitlements(db, company)[feature]:
        raise PaymentRequired(f"{what} comes with the Starter and Pro plans. See Billing to upgrade.")


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def _company_for(db: Session, user: User) -> Company:
    m = db.scalar(select(CompanyMember).where(CompanyMember.user_id == user.id))
    if m is None:
        raise HTTPException(409, "Set up your company profile first.")
    return m.company


def order_out(o: Order) -> dict:
    return {"id": o.id, "number": o.number, "product_code": o.product_code, "product": o.product_name,
            "amount_php": o.amount_php, "status": o.status, "provider": o.provider, "method": o.method,
            "card": f"{o.card_brand} ending {o.card_last4}" if o.card_last4 else None, "job_id": o.job_id,
            "failure_reason": o.failure_reason, "created_at": aware(o.created_at), "paid_at": aware(o.paid_at),
            "refunded_at": aware(o.refunded_at), "is_renewal": o.is_renewal, "test": True, "notice": RECEIPT_NOTICE,
            "buyer": o.buyer or "employer", "subtotal_php": o.subtotal_php if o.subtotal_php is not None else o.amount_php,
            "discount_code": o.discount_code, "discount_php": o.discount_php or 0,
            "invoice_url": f"/invoice.html?order={o.id}" if o.status in ("paid", "refunded") else None}


def _new_order(db: Session, company: Company | None, user: User | None, p: Product, job_id: int | None,
               renewal: bool = False) -> Order:
    o = Order(number="pending", company_id=company.id if company else None, user_id=user.id if user else None,
              buyer="employer" if company else "seeker", product_code=p.code, product_name=p.name, job_id=job_id,
              amount_php=p.price_php, subtotal_php=p.price_php, discount_php=0, provider=settings.payment_mode,
              is_renewal=renewal, test=True, bill_to={})
    db.add(o)
    db.flush()
    o.number = f"SJ-TEST-{o.id:06d}"
    return o


def fulfill(db: Session, o: Order, now: datetime | None = None) -> None:
    """Give the employer what they paid for. Safe to call twice (webhooks can repeat)."""
    if o.status == "paid":
        return
    now = now or utcnow()
    o.status, o.paid_at, o.failure_reason = "paid", now, None
    p = db.get(Product, o.product_code)
    o.bill_to = bill_to_snapshot(db, o)
    if o.discount_code:
        dc = db.get(DiscountCode, o.discount_code)
        if dc is not None:
            dc.uses += 1
    if p.kind in SEEKER_KINDS:
        prof = db.get(CandidateProfile, o.user_id)
        if p.kind == "seeker_highlight":
            base = aware(prof.highlighted_until) if prof.highlighted_until and aware(prof.highlighted_until) > now else now
            prof.highlighted_until = base + timedelta(days=p.days)
        else:
            prof.priority_credits = (prof.priority_credits or 0) + p.credits
        _tell_seeker(db, o, f"Payment received for {o.product_name} ({o.number}). {RECEIPT_NOTICE}")
        return
    company = db.get(Company, o.company_id)
    if p.kind == "job_credit":
        db.add(CreditEntry(company_id=company.id, delta=p.credits, reason=f"bought: {p.name}", order_id=o.id))
    elif p.kind == "featured":
        job = db.get(Job, o.job_id)
        if job is not None:
            base = aware(job.featured_until) if job.featured_until and aware(job.featured_until) > now else now
            job.featured_until = base + timedelta(days=p.days)
    elif p.kind == "subscription":
        sub = db.scalar(select(Subscription).where(Subscription.company_id == company.id,
                                                   Subscription.status.in_(("active", "past_due")))
                        .order_by(Subscription.id.desc()))
        if sub is not None and sub.product_code == p.code and o.is_renewal:
            start = max(aware(sub.current_period_end), now - timedelta(days=GRACE_DAYS))
            sub.current_period_start, sub.current_period_end, sub.status = start, start + timedelta(days=p.days), "active"
        else:
            if sub is not None:                    # switching plans: the old one ends now (no proration in this version)
                sub.status = "cancelled"
            sub = Subscription(company_id=company.id, product_code=p.code, status="active", started_at=now,
                               current_period_start=now, current_period_end=now + timedelta(days=p.days))
            db.add(sub)
        if o.card_last4:
            sub.renew_brand, sub.renew_last4 = o.card_brand, o.card_last4
            sub.renew_outcome = TEST_CARDS.get(_saved_test_number(o.card_brand, o.card_last4), {}).get("outcome", "success")
    _tell_company(db, company, f"Payment received for {o.product_name} ({o.number}). {RECEIPT_NOTICE}",
                  f"Receipt {o.number}: {o.product_name}", _receipt_body(o, "/employer.html#billing"))


def _receipt_body(o: Order, back: str) -> str:
    disc = f"Discount ({o.discount_code}): -PHP {o.discount_php:,}\n" if o.discount_php else ""
    return (f"Thank you. We received your payment for {o.product_name}.\n\nOrder: {o.number}\n{disc}Amount: PHP {o.amount_php:,}\n"
            f"Method: {o.method}{f' ({o.card_brand} ending {o.card_last4})' if o.card_last4 else ''}\n\n{RECEIPT_NOTICE}\n"
            f"{TEST_NOTICE}\n\nPrintable invoice: {settings.public_url}/invoice.html?order={o.id}\n"
            f"See your billing: {settings.public_url}{back}\n")


def _tell_seeker(db: Session, o: Order, note: str, subject: str | None = None, body: str | None = None) -> None:
    u = db.get(User, o.user_id)
    if u is None:
        return
    db.add(Notification(user_id=u.id, text=note[:500], link="/seeker.html#extras"))
    queue_email(db, u.email, subject or f"Receipt {o.number}: {o.product_name}", body or _receipt_body(o, "/seeker.html#extras"))


def bill_to_snapshot(db: Session, o: Order) -> dict:
    """Who the invoice is addressed to, frozen when the order is paid."""
    if o.buyer == "seeker":
        u = db.get(User, o.user_id)
        prof = db.get(CandidateProfile, o.user_id) if u else None
        return {"name": (prof.full_name if prof and prof.full_name else (u.name if u else "")), "address": prof.location if prof else "",
                "tin": "", "email": u.email if u else ""}
    c = db.get(Company, o.company_id)
    owner = db.scalar(select(User).join(CompanyMember, CompanyMember.user_id == User.id)
                      .where(CompanyMember.company_id == c.id, CompanyMember.role == "owner"))
    return {"name": c.billing_name or c.name, "address": c.billing_address or c.location_text or "", "tin": c.billing_tin or "",
            "email": owner.email if owner else ""}


def _saved_test_number(brand: str | None, last4: str | None) -> str:
    for number, info in TEST_CARDS.items():
        if number.endswith(last4 or "") and info["brand"] == brand:
            return number
    return ""


def _tell_company(db: Session, company: Company, note: str, subject: str, body: str) -> None:
    for m in db.scalars(select(CompanyMember).where(CompanyMember.company_id == company.id)):
        db.add(Notification(user_id=m.user_id, text=note[:500], link="/employer.html#billing"))
        if m.role == "owner":
            queue_email(db, m.user.email, subject, body)


def _fail(db: Session, o: Order, reason: str) -> None:
    o.status, o.failure_reason = "failed", reason


# ---------------------------------------------------------------------------
# PayMongo (test mode only)
# ---------------------------------------------------------------------------

PAYMONGO_API = "https://api.paymongo.com/v2/checkout_sessions"


def paymongo_checkout(o: Order) -> str:
    body = {"data": {"attributes": {
        "line_items": [{"name": o.product_name, "amount": o.amount_php * 100, "currency": "PHP", "quantity": 1}],
        "payment_method_types": settings.paymongo_methods,
        "success_url": f"{settings.public_url}/checkout.html?order={o.id}&return=success",
        "cancel_url": f"{settings.public_url}/checkout.html?order={o.id}&return=cancel",
        "reference_number": o.number, "description": f"Sinag Jobs (TEST) {o.product_name}"}}}
    try:
        with httpx.Client(timeout=20, transport=_transport) as client:
            r = client.post(PAYMONGO_API, json=body, auth=(settings.paymongo_secret_key, ""))
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Couldn't reach PayMongo. Try again in a minute.") from exc
    if r.status_code >= 300:
        log.warning("PayMongo checkout error %s: %s", r.status_code, r.text[:300])
        raise HTTPException(502, "PayMongo couldn't start the test checkout. Check PAYMONGO_SECRET_KEY and PAYMONGO_METHODS.")
    data = r.json()["data"]
    o.provider_ref = data["id"]
    return data["attributes"]["checkout_url"]


def verify_paymongo_signature(raw: bytes, header: str, secret: str, now: datetime | None = None,
                              tolerance: int = 600) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    t, te = parts.get("t", ""), parts.get("te", "")
    if not (t.isdigit() and te and secret):
        return False
    expected = hmac.new(secret.encode(), f"{t}.".encode() + raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, te):
        return False
    now_ts = (now or utcnow()).timestamp()
    return abs(now_ts - int(t)) <= tolerance


# ---------------------------------------------------------------------------
# Employer routes
# ---------------------------------------------------------------------------

@router.get("/summary")
def summary(user: User = Depends(require_employer), db: Session = Depends(get_session)):
    company = _company_for(db, user)
    ensure_products(db)
    orders = db.scalars(select(Order).where(Order.company_id == company.id).order_by(Order.id.desc()).limit(30)).all()
    products = db.scalars(select(Product).where(Product.active.is_(True)).order_by(Product.sort)).all()
    return {"notice": TEST_NOTICE, "mode": settings.payment_mode, "problem": config_problem(),
            "test_cards": _test_cards(),
            "products": [product_out(p) for p in products if audience_of(p) == "employer"], **entitlements(db, company),
            "billing_details": {"name": company.billing_name or "", "address": company.billing_address or "",
                                "tin": company.billing_tin or ""},
            "orders": [order_out(o) for o in orders]}


def _test_cards() -> list[dict]:
    return [{"number": n, **i} for n, i in TEST_CARDS.items()] if settings.payment_mode == "sandbox" else []


@router.get("/seeker")
def seeker_summary(user: User = Depends(require_seeker), db: Session = Depends(get_session)):
    ensure_products(db)
    prof = db.get(CandidateProfile, user.id)
    orders = db.scalars(select(Order).where(Order.user_id == user.id, Order.buyer == "seeker").order_by(Order.id.desc()).limit(30)).all()
    products = db.scalars(select(Product).where(Product.active.is_(True)).order_by(Product.sort)).all()
    now = utcnow()
    hl = aware(prof.highlighted_until) if prof and prof.highlighted_until else None
    return {"notice": TEST_NOTICE, "billing_enabled": settings.billing_enabled, "mode": settings.payment_mode,
            "problem": config_problem(), "test_cards": _test_cards(),
            "products": [product_out(p) for p in products if audience_of(p) == "seeker"],
            "highlighted_until": hl if hl and hl > now else None, "priority_credits": prof.priority_credits if prof else 0,
            "searchable": bool(prof and prof.searchable), "orders": [order_out(o) for o in orders]}


class BillingDetailsIn(BaseModel):
    name: str = Field("", max_length=200)
    address: str = Field("", max_length=400)
    tin: str = Field("", max_length=30, pattern=r"^[0-9\- ]*$")


@router.put("/details")
def set_billing_details(body: BillingDetailsIn, user: User = Depends(require_employer), db: Session = Depends(get_session)):
    """Name, address and TIN printed on invoices."""
    company = _company_for(db, user)
    company.billing_name, company.billing_address, company.billing_tin = (body.name.strip() or None, body.address.strip() or None,
                                                                          body.tin.strip() or None)
    db.commit()
    return {"ok": True, "message": "Invoice details saved. They appear on invoices for future payments."}


class CheckoutIn(BaseModel):
    product_code: str
    job_id: int | None = None
    discount_code: str | None = Field(None, max_length=40)


@router.post("/checkout", status_code=201)
def checkout(body: CheckoutIn, background: BackgroundTasks, user: User = Depends(current_user),
             db: Session = Depends(get_session)):
    _need_billing()
    ensure_products(db)
    p = db.get(Product, body.product_code)
    if p is None or not p.active:
        raise HTTPException(404, "That product isn't available.")
    if user.role == "seeker":
        if audience_of(p) != "seeker":
            raise HTTPException(404, "That product isn't available.")
        prof = db.get(CandidateProfile, user.id)
        if prof is None:
            raise HTTPException(409, "Fill in your profile first.")
        if p.kind == "seeker_highlight" and not prof.searchable:
            raise HTTPException(409, "Turn on \"Let employers find me\" in your profile first. A highlight only shows in employers' searches.")
        o = _new_order(db, None, user, p, None)
        return _finish_checkout(db, o, p, user, body.discount_code, background)
    if user.role != "employer":
        raise HTTPException(403, "Only employers and job seekers can buy.")
    company = _company_for(db, user)
    if audience_of(p) != "employer":
        raise HTTPException(404, "That product isn't available.")
    job_id = None
    if p.kind == "featured":
        job = db.get(Job, body.job_id or -1)
        if job is None or job.company_id != company.id or job.origin != "direct" or job.status != "open":
            raise HTTPException(422, "Pick one of your published jobs to feature.")
        job_id = job.id
    if p.kind == "subscription":
        sub = active_subscription(db, company.id)
        if sub is not None and sub.product_code == p.code and sub.status == "active":
            raise HTTPException(409, f"You're already on the {p.name}.")
    o = _new_order(db, company, user, p, job_id)
    return _finish_checkout(db, o, p, user, body.discount_code, background)


def _finish_checkout(db: Session, o: Order, p: Product, user: User, code: str | None, background) -> dict:
    if code and code.strip():
        apply_discount(db, o, p, code)
    if o.amount_php == 0:                      # fully discounted: nothing to pay
        o.method = "discount"
        fulfill(db, o)
        url = f"/checkout.html?order={o.id}"
    else:
        url = f"/checkout.html?order={o.id}" if settings.payment_mode == "sandbox" else paymongo_checkout(o)
    db.commit()
    flush_soon(background)
    return {"order": order_out(o), "checkout_url": url, "notice": TEST_NOTICE}


def _own_order(db: Session, user: User, order_id: int) -> Order:
    o = db.get(Order, order_id)
    if o is None:
        raise HTTPException(404, "Order not found.")
    if o.buyer == "seeker":
        if o.user_id != user.id:
            raise HTTPException(404, "Order not found.")
    else:
        m = db.scalar(select(CompanyMember).where(CompanyMember.user_id == user.id))
        if m is None or o.company_id != m.company_id:
            raise HTTPException(404, "Order not found.")
    return o


@router.get("/orders/{order_id}")
def get_order(order_id: int, user: User = Depends(current_user), db: Session = Depends(get_session)):
    o = _own_order(db, user, order_id)
    job = db.get(Job, o.job_id) if o.job_id else None
    company = db.get(Company, o.company_id) if o.company_id else None
    return {**order_out(o), "company": company.name if company else user.name, "job": job.title if job else None,
            "mode": settings.payment_mode, "test_notice": TEST_NOTICE, "test_cards": _test_cards(),
            "back_url": "/seeker.html#extras" if o.buyer == "seeker" else "/employer.html#billing"}


# ---------------------------------------------------------------------------
# Discount codes
# ---------------------------------------------------------------------------

def _code_problem(db: Session, dc: DiscountCode | None, o: Order, p: Product, now: datetime) -> str | None:
    if dc is None or not dc.active:
        return "That discount code isn't valid."
    if dc.valid_from and aware(dc.valid_from) > now:
        return "That discount code isn't active yet."
    if dc.valid_until and aware(dc.valid_until) < now:
        return "That discount code has expired."
    if dc.audience != "any" and dc.audience != (o.buyer or "employer"):
        return "That discount code isn't for this kind of account."
    if dc.product_codes and p.code not in dc.product_codes:
        return "That discount code doesn't apply to this item."
    if dc.max_uses is not None and dc.uses >= dc.max_uses:
        return "That discount code has been fully used."
    if dc.once_per_account:
        who = (Order.user_id == o.user_id) if o.buyer == "seeker" else (Order.company_id == o.company_id)
        used = db.scalar(select(func.count()).select_from(Order).where(
            Order.discount_code == dc.code, Order.status.in_(("paid", "refunded")), who, Order.id != o.id))
        if used:
            return "You've already used this discount code."
    return None


def apply_discount(db: Session, o: Order, p: Product, code: str) -> None:
    dc = db.get(DiscountCode, code.strip().upper())
    problem = _code_problem(db, dc, o, p, utcnow())
    if problem:
        raise HTTPException(422, problem)
    sub = o.subtotal_php if o.subtotal_php is not None else p.price_php
    off = round(sub * dc.percent_off / 100) if dc.percent_off else min(dc.amount_off_php or 0, sub)
    o.subtotal_php, o.discount_code, o.discount_php, o.amount_php = sub, dc.code, off, sub - off


class DiscountIn(BaseModel):
    code: str = Field(min_length=1, max_length=40)


@router.post("/orders/{order_id}/discount")
def add_discount(order_id: int, body: DiscountIn, user: User = Depends(current_user), db: Session = Depends(get_session)):
    o = _own_order(db, user, order_id)
    if o.status not in ("pending", "failed") or o.is_renewal:
        raise HTTPException(400, "A discount can only be added before paying.")
    if settings.payment_mode != "sandbox":
        raise HTTPException(400, "Enter the discount code before you go to checkout.")
    apply_discount(db, o, db.get(Product, o.product_code), body.code)
    db.commit()
    return order_out(o)


@router.delete("/orders/{order_id}/discount")
def remove_discount(order_id: int, user: User = Depends(current_user), db: Session = Depends(get_session)):
    o = _own_order(db, user, order_id)
    if o.status in ("pending", "failed") and o.discount_code:
        o.amount_php, o.discount_code, o.discount_php = o.subtotal_php or o.amount_php, None, 0
        db.commit()
    return order_out(o)


@router.post("/orders/{order_id}/confirm-free")
def confirm_free(order_id: int, background: BackgroundTasks, user: User = Depends(current_user),
                 db: Session = Depends(get_session)):
    """A discount brought the price to zero: complete the order without payment."""
    _need_billing()
    o = _own_order(db, user, order_id)
    if o.status not in ("pending", "failed") or o.amount_php != 0:
        raise HTTPException(400, "This order needs a payment.")
    problem = _code_problem(db, db.get(DiscountCode, o.discount_code or ""), o, db.get(Product, o.product_code), utcnow())
    if problem:
        raise HTTPException(409, problem)
    o.method = "discount"
    fulfill(db, o)
    db.commit()
    flush_soon(background)
    return order_out(o)


# ---------------------------------------------------------------------------
# Invoices (TEST: not BIR-registered)
# ---------------------------------------------------------------------------

def invoice_out(db: Session, o: Order) -> dict:
    if o.status not in ("paid", "refunded"):
        raise HTTPException(400, "An invoice is available once the order is paid.")
    sub = o.subtotal_php if o.subtotal_php is not None else o.amount_php
    vat = None
    if settings.vat_registered:
        vatable = round(o.amount_php / 1.12, 2)
        vat = {"vatable_sales": vatable, "vat_12": round(o.amount_php - vatable, 2)}
    return {"number": o.number, "issued_at": aware(o.paid_at), "status": o.status, "refunded_at": aware(o.refunded_at),
            "seller": {"name": settings.seller_name, "address": settings.seller_address, "tin": settings.seller_tin,
                       "vat_registered": settings.vat_registered},
            "bill_to": o.bill_to or bill_to_snapshot(db, o),
            "lines": [{"description": o.product_name + (" (renewal)" if o.is_renewal else ""), "qty": 1, "unit_php": sub,
                       "amount_php": sub}],
            "subtotal_php": sub, "discount_code": o.discount_code, "discount_php": o.discount_php or 0, "total_php": o.amount_php,
            "vat": vat, "payment": {"method": o.method, "card": f"Test {o.card_brand} ending {o.card_last4}" if o.card_last4 else None},
            "notice": INVOICE_NOTICE, "test": True,
            "back_url": "/seeker.html#extras" if o.buyer == "seeker" else "/employer.html#billing"}


@router.get("/orders/{order_id}/invoice")
def my_invoice(order_id: int, user: User = Depends(current_user), db: Session = Depends(get_session)):
    return invoice_out(db, _own_order(db, user, order_id))


class SandboxPayIn(BaseModel):
    method: str = Field(pattern="^(card|gcash|maya)$")
    card_number: str | None = Field(None, max_length=30)
    exp_month: int | None = Field(None, ge=1, le=12)
    exp_year: int | None = Field(None, ge=2000, le=2100)
    cvc: str | None = Field(None, max_length=4)
    outcome: str = Field("success", pattern="^(success|fail)$")     # for simulated e-wallets


@router.post("/orders/{order_id}/sandbox-pay")
def sandbox_pay(order_id: int, body: SandboxPayIn, background: BackgroundTasks, user: User = Depends(current_user),
                db: Session = Depends(get_session)):
    _need_billing()
    if settings.payment_mode != "sandbox":
        raise HTTPException(400, "The payment simulator is only used when PAYMENT_MODE=sandbox.")
    o = _own_order(db, user, order_id)
    if o.status == "paid":
        raise HTTPException(409, "This order is already paid.")
    if o.status in ("refunded", "cancelled"):
        raise HTTPException(400, "This order is closed. Start a new checkout.")
    if o.discount_code:
        problem = _code_problem(db, db.get(DiscountCode, o.discount_code), o, db.get(Product, o.product_code), utcnow())
        if problem:
            o.amount_php, o.discount_code, o.discount_php = o.subtotal_php or o.amount_php, None, 0
            db.commit()
            raise HTTPException(409, problem + " It was removed from your order; check the new total.")
    if o.amount_php == 0:
        raise HTTPException(400, "Nothing to pay. Use Confirm order instead.")
    o.method = body.method
    if body.method == "card":
        digits = re.sub(r"\D", "", body.card_number or "")
        card = TEST_CARDS.get(digits)
        if card is None:
            # Never store, log or echo what was typed.
            if 13 <= len(digits) <= 19 and luhn_ok(digits):
                raise HTTPException(422, "That looks like a real card number. " + TEST_NOTICE +
                                    " Use one of the test card numbers shown on this page.")
            raise HTTPException(422, "That isn't one of the test card numbers. Use one shown on this page.")
        now = utcnow()
        if not body.exp_month or not body.exp_year or (body.exp_year, body.exp_month) < (now.year, now.month):
            raise HTTPException(422, "Enter any future expiry date, e.g. 12/30.")
        if not (body.cvc or "").isdigit() or len(body.cvc) not in (3, 4):
            raise HTTPException(422, "Enter any 3-digit CVC, e.g. 123.")
        o.card_brand, o.card_last4 = card["brand"], digits[-4:]
        if card["outcome"] != "success":
            _fail(db, o, FAILURES[card["outcome"]])
            db.commit()
            return order_out(o)
    elif body.outcome == "fail":
        _fail(db, o, f"The simulated {'GCash' if body.method == 'gcash' else 'Maya'} payment was cancelled.")
        db.commit()
        return order_out(o)
    fulfill(db, o)
    db.commit()
    flush_soon(background)
    return order_out(o)


@router.post("/orders/{order_id}/cancel")
def cancel_order(order_id: int, user: User = Depends(current_user), db: Session = Depends(get_session)):
    o = _own_order(db, user, order_id)
    if o.status in ("pending", "failed"):
        o.status = "cancelled"
        db.commit()
    return order_out(o)


@router.post("/use-boost")
def use_plan_boost(body: CheckoutIn, user: User = Depends(require_employer), db: Session = Depends(get_session)):
    """Feature a job with one of the plan's included boosts (no payment)."""
    company = _company_for(db, user)
    ent = entitlements(db, company)
    if ent["featured_boosts_left"] <= 0:
        raise PaymentRequired("No featured boosts left this month. Buy a featured listing instead.")
    job = db.get(Job, body.job_id or -1)
    if job is None or job.company_id != company.id or job.origin != "direct" or job.status != "open":
        raise HTTPException(422, "Pick one of your published jobs to feature.")
    now = utcnow()
    base = aware(job.featured_until) if job.featured_until and aware(job.featured_until) > now else now
    job.featured_until = base + timedelta(days=7)
    db.add(CreditEntry(company_id=company.id, delta=0, reason="plan featured boost", job_id=job.id))
    db.commit()
    return {"ok": True, "featured_until": aware(job.featured_until)}


@router.post("/subscription/cancel")
def cancel_subscription(user: User = Depends(require_employer), db: Session = Depends(get_session)):
    company = _company_for(db, user)
    sub = active_subscription(db, company.id)
    if sub is None:
        raise HTTPException(404, "You don't have an active plan.")
    sub.cancel_at_period_end = True
    db.commit()
    return {"ok": True, "message": f"Your plan stays active until {aware(sub.current_period_end):%b %d, %Y}, then ends."}


@router.post("/subscription/resume")
def resume_subscription(user: User = Depends(require_employer), db: Session = Depends(get_session)):
    company = _company_for(db, user)
    sub = active_subscription(db, company.id)
    if sub is None:
        raise HTTPException(404, "You don't have an active plan.")
    sub.cancel_at_period_end = False
    db.commit()
    return {"ok": True, "message": "Your plan will renew."}


class RenewCardIn(BaseModel):
    card_number: str = Field(max_length=30)


@router.post("/subscription/card")
def set_renewal_card(body: RenewCardIn, user: User = Depends(require_employer), db: Session = Depends(get_session)):
    """Sandbox only: choose which TEST card renewals use (e.g. a declining one, to try a failed renewal)."""
    _need_billing()
    if settings.payment_mode != "sandbox":
        raise HTTPException(400, "Renewal cards are managed by PayMongo in this mode.")
    digits = re.sub(r"\D", "", body.card_number)
    card = TEST_CARDS.get(digits)
    if card is None:
        raise HTTPException(422, ("That looks like a real card number. " + TEST_NOTICE) if 13 <= len(digits) <= 19 and luhn_ok(digits)
                            else "That isn't one of the test card numbers.")
    company = _company_for(db, user)
    sub = active_subscription(db, company.id)
    if sub is None:
        raise HTTPException(404, "You don't have an active plan.")
    sub.renew_brand, sub.renew_last4, sub.renew_outcome = card["brand"], digits[-4:], card["outcome"]
    db.commit()
    return {"ok": True, "message": f"Renewals will use the test {card['brand']} ending {digits[-4:]} ({card['label'].lower()})."}


@router.post("/webhooks/paymongo")
async def paymongo_webhook(request: Request, background: BackgroundTasks, db: Session = Depends(get_session)):
    raw = await request.body()
    if not verify_paymongo_signature(raw, request.headers.get("paymongo-signature", ""), settings.paymongo_webhook_secret):
        raise HTTPException(400, "Invalid signature.")
    event = json.loads(raw)
    attrs = event.get("data", {}).get("attributes", {})
    if attrs.get("livemode"):
        log.warning("ignored a LIVE mode PayMongo event; this site only accepts test payments")
        return {"ok": True, "ignored": "live mode"}
    if attrs.get("type") != "checkout_session.payment.paid":
        return {"ok": True, "ignored": attrs.get("type")}
    session = attrs.get("data", {})
    ref = session.get("attributes", {}).get("reference_number")
    o = db.scalar(select(Order).where((Order.provider_ref == session.get("id")) | (Order.number == (ref or "-"))))
    if o is None:
        return {"ok": True, "ignored": "unknown order"}
    payments = session.get("attributes", {}).get("payments") or [{}]
    src = (payments[0].get("attributes", {}) or {}).get("source", {}) or {}
    o.method = src.get("type") or "paymongo"
    if src.get("last4"):
        o.card_brand, o.card_last4 = (src.get("brand") or "Card").title(), src["last4"]
    fulfill(db, o)
    db.commit()
    flush_soon(background)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Renewals and expiry (run by the worker)
# ---------------------------------------------------------------------------

def run_renewals(session_factory, now: datetime | None = None) -> dict:
    now = now or utcnow()
    out = {"renewed": 0, "failed": 0, "expired": 0}
    with session_factory() as db:
        subs = db.scalars(select(Subscription).where(Subscription.status.in_(("active", "past_due")),
                                                     Subscription.current_period_end <= now)).all()
        for sub in subs:
            company = db.get(Company, sub.company_id)
            plan = db.get(Product, sub.product_code)
            if sub.cancel_at_period_end or plan is None or not plan.active:
                _expire(db, sub, company, now, "Your plan has ended.")
                out["expired"] += 1
                continue
            if sub.status == "past_due":
                if aware(sub.current_period_end) + timedelta(days=GRACE_DAYS) <= now:
                    _expire(db, sub, company, now, "Your plan ended because the renewal wasn't paid.")
                    out["expired"] += 1
                continue
            o = _new_order(db, company, None, plan, None, renewal=True)
            if settings.payment_mode == "sandbox" and sub.renew_last4:
                o.method, o.card_brand, o.card_last4 = "renewal", sub.renew_brand, sub.renew_last4
                if (sub.renew_outcome or "success") == "success":
                    fulfill(db, o, now)
                    out["renewed"] += 1
                    continue
                _fail(db, o, FAILURES.get(sub.renew_outcome, "The saved test card was declined."))
            sub.status = "past_due"
            out["failed"] += 1
            _tell_company(db, company, f"Your {plan.name} renewal wasn't paid. Pay within {GRACE_DAYS} days to keep it.",
                          f"Action needed: renew your {plan.name}",
                          f"We couldn't renew your {plan.name} ({o.number}). Pay within {GRACE_DAYS} days to keep your "
                          f"plan: {settings.public_url}/employer.html#billing\n\n{TEST_NOTICE}\n")
        db.commit()
    return out


def _expire(db: Session, sub: Subscription, company: Company, now: datetime, msg: str) -> None:
    sub.status = "expired"
    free_in_use = db.scalar(select(func.count()).select_from(Job).where(
        Job.company_id == company.id, Job.paid_via == "free", Job.status == "open")) or 0
    keep = max(0, settings.free_live_jobs - free_in_use)      # only fill an unused free slot
    jobs = db.scalars(select(Job).where(Job.company_id == company.id, Job.paid_via == "subscription",
                                        Job.status == "open").order_by(Job.posted_at)).all()
    for j in jobs[keep:]:
        j.status, j.closed_at = "closed", now
    for j in jobs[:keep]:
        j.paid_via = "free"
    closed = max(0, len(jobs) - keep)
    _tell_company(db, company, msg + (f" {closed} job post(s) were closed." if closed else ""), "Your Sinag Jobs plan ended",
                  msg + (f"\n\n{closed} job post(s) beyond the free allowance were closed." if closed else "")
                  + f"\n\nChoose a plan again any time: {settings.public_url}/employer.html#billing\n")


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

@admin_router.get("")
def admin_billing(status: str | None = None, db: Session = Depends(get_session)):
    ensure_products(db)
    q = select(Order).order_by(Order.id.desc()).limit(200)
    if status:
        q = q.where(Order.status == status)
    orders = db.scalars(q).all()
    names = {c.id: c.name for c in db.scalars(select(Company).where(Company.id.in_({o.company_id for o in orders if o.company_id} or {-1})))}
    people = {u.id: u.name for u in db.scalars(select(User).where(User.id.in_({o.user_id for o in orders if o.buyer == "seeker"} or {-1})))}
    paid = db.execute(select(Order.product_name, func.count(), func.sum(Order.amount_php))
                      .where(Order.status == "paid").group_by(Order.product_name)).all()
    return {"notice": TEST_NOTICE, "mode": settings.payment_mode, "problem": config_problem(),
            "products": [product_out(p) for p in db.scalars(select(Product).order_by(Product.sort))],
            "revenue_by_product": [{"product": n, "orders": c, "amount_php": a or 0} for n, c, a in paid],
            "active_subscriptions": db.scalar(select(func.count()).select_from(Subscription).where(Subscription.status == "active")),
            "discounts": [discount_out(d) for d in db.scalars(select(DiscountCode).order_by(DiscountCode.created_at.desc()))],
            "orders": [{**order_out(o), "company": names.get(o.company_id) if o.buyer != "seeker" else f"{people.get(o.user_id, '?')} (job seeker)"}
                       for o in orders]}


def discount_out(d: DiscountCode) -> dict:
    return {"code": d.code, "description": d.description, "percent_off": d.percent_off, "amount_off_php": d.amount_off_php,
            "audience": d.audience, "product_codes": d.product_codes or [], "max_uses": d.max_uses, "uses": d.uses,
            "once_per_account": d.once_per_account, "valid_from": aware(d.valid_from), "valid_until": aware(d.valid_until),
            "active": d.active}


class DiscountCodeIn(BaseModel):
    code: str = Field(min_length=3, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    description: str = Field("", max_length=200)
    percent_off: int | None = Field(None, ge=1, le=100)
    amount_off_php: int | None = Field(None, ge=1, le=1_000_000)
    audience: str = Field("any", pattern="^(any|employer|seeker)$")
    product_codes: list[str] = Field(default_factory=list, max_length=20)
    max_uses: int | None = Field(None, ge=1, le=1_000_000)
    once_per_account: bool = True
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    active: bool = True


@admin_router.post("/discounts", status_code=201)
def create_discount(body: DiscountCodeIn, db: Session = Depends(get_session)):
    if (body.percent_off is None) == (body.amount_off_php is None):
        raise HTTPException(422, "Set either a percentage or a peso amount off, not both.")
    code = body.code.upper()
    existing = db.get(DiscountCode, code)
    if existing is not None and existing.uses:
        raise HTTPException(409, "That code was already used. Pick a new code, or edit only its end date and on/off switch.")
    d = existing or DiscountCode(code=code)
    for k, v in body.model_dump().items():
        if k != "code":
            setattr(d, k, v)
    db.add(d)
    db.commit()
    return discount_out(d)


class DiscountPatch(BaseModel):
    active: bool | None = None
    valid_until: datetime | None = None
    max_uses: int | None = Field(None, ge=1, le=1_000_000)


@admin_router.patch("/discounts/{code}")
def update_discount(code: str, body: DiscountPatch, db: Session = Depends(get_session)):
    d = db.get(DiscountCode, code.upper())
    if d is None:
        raise HTTPException(404, "Code not found.")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(d, k, v)
    db.commit()
    return discount_out(d)


@admin_router.get("/orders/{order_id}/invoice")
def admin_invoice(order_id: int, db: Session = Depends(get_session)):
    o = db.get(Order, order_id)
    if o is None:
        raise HTTPException(404, "Order not found.")
    return invoice_out(db, o)


class ProductIn(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    description: str = Field("", max_length=300)
    price_php: int = Field(ge=0, le=1_000_000)
    credits: int = Field(0, ge=0, le=1000)
    days: int = Field(0, ge=0, le=366)
    limits: dict = Field(default_factory=dict)
    active: bool = True


@admin_router.put("/products/{code}")
def update_product(code: str, body: ProductIn, db: Session = Depends(get_session)):
    ensure_products(db)
    p = db.get(Product, code)
    if p is None:
        raise HTTPException(404, "Product not found.")
    for k, v in body.model_dump().items():
        if k == "limits" and p.kind != "subscription":
            continue
        setattr(p, k, v)
    db.commit()
    return product_out(p)


@admin_router.post("/orders/{order_id}/refund")
def refund(order_id: int, background: BackgroundTasks, db: Session = Depends(get_session)):
    o = db.get(Order, order_id)
    if o is None:
        raise HTTPException(404, "Order not found.")
    if o.status != "paid":
        raise HTTPException(400, "Only paid orders can be refunded.")
    p = db.get(Product, o.product_code)
    now = utcnow()
    o.status, o.refunded_at = "refunded", now
    if p.kind in SEEKER_KINDS:
        prof = db.get(CandidateProfile, o.user_id)
        if prof is not None:
            if p.kind == "seeker_highlight":
                prof.highlighted_until = None
            else:
                prof.priority_credits = max(0, (prof.priority_credits or 0) - p.credits)
        _tell_seeker(db, o, f"Order {o.number} was refunded (test refund).", f"Refund: {o.number}",
                     f"Order {o.number} ({o.product_name}, PHP {o.amount_php:,}) was refunded.\n\n{RECEIPT_NOTICE}\n")
        db.commit()
        flush_soon(background)
        return order_out(o)
    company = db.get(Company, o.company_id)
    if p.kind == "job_credit":
        take = min(p.credits, max(0, credit_balance(db, company.id)))
        if take:
            db.add(CreditEntry(company_id=company.id, delta=-take, reason=f"refund {o.number}", order_id=o.id))
    elif p.kind == "featured" and o.job_id:
        job = db.get(Job, o.job_id)
        if job is not None:
            job.featured_until = None
    elif p.kind == "subscription":
        sub = active_subscription(db, company.id)
        if sub is not None and sub.product_code == p.code:
            _expire(db, sub, company, now, f"Your {p.name} was refunded and has ended.")
    _tell_company(db, company, f"Order {o.number} was refunded (test refund).", f"Refund: {o.number}",
                  f"Order {o.number} ({o.product_name}, PHP {o.amount_php:,}) was refunded.\n\n{RECEIPT_NOTICE}\n")
    db.commit()
    flush_soon(background)
    return order_out(o)


class GrantIn(BaseModel):
    credits: int = Field(ge=-100, le=100)
    reason: str = Field("Added by admin", max_length=100)


@admin_router.post("/companies/{company_id}/credits")
def grant_credits(company_id: int, body: GrantIn, db: Session = Depends(get_session)):
    if db.get(Company, company_id) is None:
        raise HTTPException(404, "Company not found.")
    db.add(CreditEntry(company_id=company_id, delta=body.credits, reason=body.reason))
    db.commit()
    return {"ok": True, "credits": credit_balance(db, company_id)}
