"""Email: every message is saved to the outbox table first, then sent over SMTP.

Saving first means a slow or broken email service never breaks a page, and failed sends are
retried by the worker. With no SMTP_HOST set (on your own computer), emails are only logged;
read them with `python -m app.cli outbox`.
"""
from __future__ import annotations

import logging
import smtplib
from datetime import timedelta
from email.message import EmailMessage

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from .config import settings
from .models import OutboxEmail, utcnow

log = logging.getLogger("sinag.mailer")
MAX_ATTEMPTS = 5
default_factory: sessionmaker | None = None   # tests point this at their own database


def queue_email(db: Session, to: str, subject: str, body: str, attachments: list[dict] | None = None) -> OutboxEmail:
    """attachments: [{"filename": "invite.ics", "content_type": "text/calendar; method=REQUEST", "text": "..."}]"""
    msg = OutboxEmail(to=to, subject=subject, body=body, attachments=attachments or [])
    db.add(msg)
    return msg


def _send(msg: OutboxEmail) -> None:
    em = EmailMessage()
    em["From"] = settings.smtp_from
    em["To"] = msg.to
    em["Subject"] = msg.subject
    em.set_content(msg.body)
    for a in msg.attachments or []:
        maintype, _, rest = a["content_type"].partition("/")
        subtype, _, params = rest.partition(";")
        extra = {}
        if "method=" in params:                     # calendar invites: lets mail apps show Accept/Decline
            extra["params"] = {"method": params.split("method=")[1].strip()}
        em.add_attachment(a["text"].encode(), maintype=maintype, subtype=subtype.strip(), filename=a["filename"], **extra)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
        if settings.smtp_starttls:
            smtp.starttls()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(em)


def flush_outbox(session_factory: sessionmaker | None = None, limit: int = 50) -> int:
    """Send waiting emails. Safe to call from several processes: each email is claimed first."""
    from .db import SessionLocal
    factory = session_factory or default_factory or SessionLocal
    sent = 0
    with factory() as db:
        stale = utcnow() - timedelta(minutes=10)
        ids = db.scalars(select(OutboxEmail.id).where(
            OutboxEmail.sent_at.is_(None), OutboxEmail.attempts < MAX_ATTEMPTS,
            or_(OutboxEmail.claimed_at.is_(None), OutboxEmail.claimed_at < stale))
            .order_by(OutboxEmail.id).limit(limit)).all()
        for mid in ids:
            claimed = db.execute(update(OutboxEmail).where(
                OutboxEmail.id == mid, OutboxEmail.sent_at.is_(None),
                or_(OutboxEmail.claimed_at.is_(None), OutboxEmail.claimed_at < stale))
                .values(claimed_at=utcnow())).rowcount
            db.commit()
            if not claimed:
                continue
            msg = db.get(OutboxEmail, mid)
            msg.attempts += 1
            if not settings.smtp_host:
                log.info("EMAIL (not sent, SMTP_HOST is empty) to=%s subject=%s\n%s", msg.to, msg.subject, msg.body)
                msg.sent_at = utcnow()
                msg.error = "Not sent: SMTP_HOST is not set (logged only)"
            else:
                try:
                    _send(msg)
                    msg.sent_at = utcnow()
                    msg.error = None
                    sent += 1
                except Exception as exc:   # keep for retry
                    msg.error = f"{type(exc).__name__}: {exc}"[:1000]
                    msg.claimed_at = None
                    log.warning("email to %s failed (attempt %d): %s", msg.to, msg.attempts, exc)
            db.commit()
    return sent


def flush_soon(background) -> None:
    """Send right after the response goes out (FastAPI BackgroundTasks)."""
    if background is not None:
        from .push import flush_push
        background.add_task(flush_outbox)
        background.add_task(flush_push)
