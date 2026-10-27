"""Database tables."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; everything is stored as UTC."""
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def split_pipes(value: str | None) -> list[str]:
    return [p for p in (value or "").split("|") if p]


def join_pipes(values) -> str:
    vals = [v.strip() for v in values or [] if v and v.strip()]
    return "|" + "|".join(dict.fromkeys(vals)) + "|" if vals else ""


# ---------------------------------------------------------------------------
# Companies and jobs
# ---------------------------------------------------------------------------

class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    # greenhouse | lever | ashby | workable | smartrecruiters | jsonld | direct (employer account on Sinag)
    source: Mapped[str] = mapped_column(String(40))
    source_key: Mapped[str] = mapped_column(String(500))
    careers_url: Mapped[str | None] = mapped_column(String(500))
    logo_url: Mapped[str | None] = mapped_column(String(500))
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    verified: Mapped[bool] = mapped_column(Boolean, default=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    industry: Mapped[str | None] = mapped_column(String(80), index=True)
    website: Mapped[str | None] = mapped_column(String(300))
    location_text: Mapped[str | None] = mapped_column(String(200))
    about: Mapped[str | None] = mapped_column(Text)
    # Employer companies start as "pending" until an admin approves them. Crawled companies are approved.
    approval: Mapped[str] = mapped_column(String(20), default="approved")
    approval_note: Mapped[str | None] = mapped_column(String(500))
    # Branded company page
    tagline: Mapped[str | None] = mapped_column(String(200))
    size: Mapped[str | None] = mapped_column(String(40))
    founded: Mapped[int | None] = mapped_column(Integer)
    cover_url: Mapped[str | None] = mapped_column(String(500))
    video_url: Mapped[str | None] = mapped_column(String(500))
    culture_values: Mapped[list | None] = mapped_column(JSON)  # [{"title", "text"}]
    testimonials: Mapped[list | None] = mapped_column(JSON)    # [{"name", "role", "quote"}]
    photos: Mapped[list | None] = mapped_column(JSON)          # [url, ...]
    links: Mapped[dict | None] = mapped_column(JSON)           # {"facebook": url, "linkedin": url, ...}
    # Invoice details (optional)
    billing_name: Mapped[str | None] = mapped_column(String(200))
    billing_address: Mapped[str | None] = mapped_column(String(400))
    billing_tin: Mapped[str | None] = mapped_column(String(30))

    last_crawled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(String(20))
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    jobs: Mapped[list["Job"]] = relationship(back_populates="company")
    members: Mapped[list["CompanyMember"]] = relationship(back_populates="company", cascade="all, delete-orphan")


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("company_id", "external_id", name="uq_job_company_external"),
        Index("ix_jobs_status_posted", "status", "posted_at"),
        Index("ix_jobs_status_salary", "status", "salary_max"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    external_id: Mapped[str] = mapped_column(String(200))
    origin: Mapped[str] = mapped_column(String(10), default="crawled")   # crawled | direct

    title: Mapped[str] = mapped_column(String(300))
    url: Mapped[str] = mapped_column(String(1000))
    apply_url: Mapped[str | None] = mapped_column(String(1000))
    department: Mapped[str | None] = mapped_column(String(200))
    employment_type: Mapped[str | None] = mapped_column(String(60))
    job_type: Mapped[str] = mapped_column(String(20), default="Not stated", index=True)

    location_raw: Mapped[str | None] = mapped_column(String(500))
    location: Mapped[str] = mapped_column(String(80), index=True)
    city: Mapped[str | None] = mapped_column(String(120))
    ph_eligibility: Mapped[str] = mapped_column(String(30))

    setup: Mapped[str] = mapped_column(String(20), index=True)
    shift: Mapped[str] = mapped_column(String(20), index=True)
    level: Mapped[str] = mapped_column(String(20), index=True)
    category: Mapped[str] = mapped_column(String(60), index=True)
    perks: Mapped[str] = mapped_column(String(500), default="")
    skills: Mapped[str] = mapped_column(String(1000), default="")     # employer-listed skills, "|a|b|"

    salary_min: Mapped[int | None] = mapped_column(Integer)
    salary_max: Mapped[int | None] = mapped_column(Integer)
    salary_original: Mapped[str | None] = mapped_column(String(200))

    description_html: Mapped[str | None] = mapped_column(Text)
    description_text: Mapped[str | None] = mapped_column(Text)

    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    missed_crawls: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(10), default="open")   # draft | open | closed
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str] = mapped_column(String(64))
    featured: Mapped[bool] = mapped_column(Boolean, default=False)

    # Direct (employer-posted) jobs
    easy_apply: Mapped[bool] = mapped_column(Boolean, default=False)
    publish_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    view_count: Mapped[int] = mapped_column(Integer, default=0)
    assessment_id: Mapped[int | None] = mapped_column(ForeignKey("assessments.id", ondelete="SET NULL"))
    assessment_auto: Mapped[bool] = mapped_column(Boolean, default=False)   # send the test right after applying
    chat_screening: Mapped[bool] = mapped_column(Boolean, default=False)
    chat_topics: Mapped[list | None] = mapped_column(JSON)                  # what the screening chat asks about
    featured_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))   # paid "featured" boost
    paid_via: Mapped[str | None] = mapped_column(String(20))   # free | credit | subscription | admin | import

    company: Mapped[Company] = relationship(back_populates="jobs")
    questions: Mapped[list["JobQuestion"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobQuestion.position")

    @property
    def perk_list(self) -> list[str]:
        return split_pipes(self.perks)

    @property
    def skill_list(self) -> list[str]:
        return split_pipes(self.skills)


class JobQuestion(Base):
    """A screening question on an employer's application form."""
    __tablename__ = "job_questions"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(String(500))
    kind: Mapped[str] = mapped_column(String(10))           # text | yesno | choice
    options: Mapped[list] = mapped_column(JSON, default=list)
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    knockout_answer: Mapped[str | None] = mapped_column(String(200))   # answer needed to pass, for yesno/choice

    job: Mapped[Job] = relationship(back_populates="questions")


class CrawlRun(Base):
    __tablename__ = "crawl_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    companies_ok: Mapped[int] = mapped_column(Integer, default=0)
    companies_failed: Mapped[int] = mapped_column(Integer, default=0)
    jobs_seen: Mapped[int] = mapped_column(Integer, default=0)
    jobs_new: Mapped[int] = mapped_column(Integer, default=0)
    jobs_updated: Mapped[int] = mapped_column(Integer, default=0)
    jobs_closed: Mapped[int] = mapped_column(Integer, default=0)
    jobs_skipped_not_ph: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list] = mapped_column(JSON, default=list)


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(10))            # seeker | employer | admin
    name: Mapped[str] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lang: Mapped[str] = mapped_column(String(5), default="en")     # en | tl | ceb

    profile: Mapped["CandidateProfile | None"] = relationship(back_populates="user", uselist=False,
                                                              cascade="all, delete-orphan")
    memberships: Mapped[list["CompanyMember"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    user: Mapped[User] = relationship()


class PasswordReset(Base):
    __tablename__ = "password_resets"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CompanyMember(Base):
    __tablename__ = "company_members"
    __table_args__ = (UniqueConstraint("company_id", "user_id", name="uq_company_member"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20), default="owner")   # owner | recruiter

    company: Mapped[Company] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="memberships")


# ---------------------------------------------------------------------------
# Job seekers
# ---------------------------------------------------------------------------

class CandidateProfile(Base):
    __tablename__ = "candidate_profiles"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    full_name: Mapped[str] = mapped_column(String(200), default="")
    headline: Mapped[str] = mapped_column(String(200), default="")
    phone: Mapped[str] = mapped_column(String(40), default="")
    location: Mapped[str] = mapped_column(String(80), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    skills: Mapped[str] = mapped_column(String(1000), default="")
    years_experience: Mapped[int | None] = mapped_column(Integer)
    desired_salary: Mapped[int | None] = mapped_column(Integer)       # PHP per month
    desired_setups: Mapped[str] = mapped_column(String(100), default="")
    desired_job_types: Mapped[str] = mapped_column(String(100), default="")
    desired_categories: Mapped[str] = mapped_column(String(300), default="")
    searchable: Mapped[bool] = mapped_column(Boolean, default=False)  # visible in the resume database (phase 2)
    highlighted_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))   # paid extra
    priority_credits: Mapped[int] = mapped_column(Integer, default=0)                      # paid extra
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(back_populates="profile")

    @property
    def skill_list(self) -> list[str]:
        return split_pipes(self.skills)


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))            # resume | certificate | portfolio | other
    filename: Mapped[str] = mapped_column(String(255))
    stored_name: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100))
    size: Mapped[int] = mapped_column(Integer)
    text: Mapped[str | None] = mapped_column(Text)            # extracted text, used for scoring
    archived: Mapped[bool] = mapped_column(Boolean, default=False)   # removed by the owner but still attached to an application
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (UniqueConstraint("job_id", "user_id", name="uq_application"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # applied | screening | shortlisted | interview | offer | hired | rejected | withdrawn
    stage: Mapped[str] = mapped_column(String(20), default="applied", index=True)
    viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    score: Mapped[int] = mapped_column(Integer, default=0)
    score_details: Mapped[dict] = mapped_column(JSON, default=dict)
    cover_letter: Mapped[str] = mapped_column(Text, default="")
    answers: Mapped[list] = mapped_column(JSON, default=list)
    profile_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    document_ids: Mapped[list] = mapped_column(JSON, default=list)
    tags: Mapped[str] = mapped_column(String(500), default="")
    ai_summary: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[bool] = mapped_column(Boolean, default=False)       # sent as a priority application (paid extra)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    job: Mapped[Job] = relationship()
    user: Mapped[User] = relationship()
    events: Mapped[list["ApplicationEvent"]] = relationship(
        back_populates="application", cascade="all, delete-orphan", order_by="ApplicationEvent.id")


class ApplicationEvent(Base):
    __tablename__ = "application_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    kind: Mapped[str] = mapped_column(String(20))            # applied | viewed | stage | note | withdrawn
    from_stage: Mapped[str | None] = mapped_column(String(20))
    to_stage: Mapped[str | None] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(Text)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    visible_to_candidate: Mapped[bool] = mapped_column(Boolean, default=False)

    application: Mapped[Application] = relationship(back_populates="events")


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    text: Mapped[str] = mapped_column(String(500))
    link: Mapped[str | None] = mapped_column(String(300))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AlertSubscription(Base):
    __tablename__ = "alert_subscriptions"
    __table_args__ = (UniqueConstraint("email", "category", "location", name="uq_alert"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    category: Mapped[str | None] = mapped_column(String(60))
    location: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    keywords: Mapped[str | None] = mapped_column(String(200))
    min_pay: Mapped[int | None] = mapped_column(Integer)
    frequency: Mapped[str] = mapped_column(String(10), default="daily")      # daily | instant
    use_profile: Mapped[bool] = mapped_column(Boolean, default=False)       # only send jobs that match the profile
    unsubscribe_token: Mapped[str | None] = mapped_column(String(64), index=True)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=True)    # sign-ups without an account confirm by email


# ---------------------------------------------------------------------------
# Email and small bits of state
# ---------------------------------------------------------------------------

class OutboxEmail(Base):
    """Every email goes here first; the mailer sends it and records the result."""
    __tablename__ = "outbox_emails"

    id: Mapped[int] = mapped_column(primary_key=True)
    to: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    attachments: Mapped[list | None] = mapped_column(JSON, default=list)   # [{"filename", "content_type", "text"}]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class AppState(Base):
    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(String(500))


# ---------------------------------------------------------------------------
# Phase 2: one-time email links, push, invites, assessments, screening chat
# ---------------------------------------------------------------------------

class EmailToken(Base):
    """One-time links sent by email: verify an address, join a team, confirm an alert."""
    __tablename__ = "email_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    purpose: Mapped[str] = mapped_column(String(20))          # verify | team | alert
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    endpoint: Mapped[str] = mapped_column(String(1000), unique=True)
    p256dh: Mapped[str] = mapped_column(String(200))
    auth: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PushMessage(Base):
    """A push notification waiting to be sent to every browser the user turned push on in."""
    __tablename__ = "push_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(String(500))
    link: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CandidateInvite(Base):
    """An employer found someone in the resume database and invited them to apply."""
    __tablename__ = "candidate_invites"
    __table_args__ = (UniqueConstraint("job_id", "user_id", name="uq_invite"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    sent_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    message: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="sent")   # sent | applied | declined
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    job: Mapped[Job] = relationship()
    company: Mapped[Company] = relationship()


class Assessment(Base):
    """A timed multiple-choice skill test an employer can attach to jobs."""
    __tablename__ = "assessments"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    instructions: Mapped[str] = mapped_column(Text, default="")
    time_limit_minutes: Mapped[int] = mapped_column(Integer, default=15)
    pass_score: Mapped[int] = mapped_column(Integer, default=60)       # percent
    questions: Mapped[list] = mapped_column(JSON, default=list)        # [{"text", "options": [...], "answer": index}]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AssessmentAttempt(Base):
    __tablename__ = "assessment_attempts"
    __table_args__ = (UniqueConstraint("application_id", "assessment_id", name="uq_attempt"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), index=True)
    assessment_id: Mapped[int] = mapped_column(ForeignKey("assessments.id", ondelete="CASCADE"), index=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answers: Mapped[list] = mapped_column(JSON, default=list)
    score: Mapped[int | None] = mapped_column(Integer)                 # percent
    passed: Mapped[bool | None] = mapped_column(Boolean)
    late: Mapped[bool] = mapped_column(Boolean, default=False)

    assessment: Mapped[Assessment] = relationship()
    application: Mapped[Application] = relationship()


class ScreeningChat(Base):
    """Follow-up questions asked as a chat after applying. Scripted, or AI-led when an API key is set."""
    __tablename__ = "screening_chats"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), unique=True)
    mode: Mapped[str] = mapped_column(String(10), default="scripted")   # scripted | ai
    messages: Mapped[list] = mapped_column(JSON, default=list)          # [{"role": "bot"|"candidate", "text", "at"}]
    topic_index: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(10), default="open")     # open | done
    summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    application: Mapped[Application] = relationship()


# ---------------------------------------------------------------------------
# Phase 3: billing (test payments only), analytics, imports
# ---------------------------------------------------------------------------

class Product(Base):
    """Something an employer can buy. Prices are editable by admins."""
    __tablename__ = "products"

    code: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(20))          # job_credit | featured | subscription | seeker_highlight | seeker_priority
    description: Mapped[str] = mapped_column(String(300), default="")
    price_php: Mapped[int] = mapped_column(Integer)          # whole pesos
    credits: Mapped[int] = mapped_column(Integer, default=0)  # job credits included
    days: Mapped[int] = mapped_column(Integer, default=0)     # featured length, or subscription period
    limits: Mapped[dict] = mapped_column(JSON, default=dict)  # subscription: live_jobs, featured_per_period, resume_db, bulk_import
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)


class Order(Base):
    """One checkout. Every order on this site is a test order: no real money moves."""
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(30), unique=True, index=True)   # SJ-TEST-000001
    # Employer orders have a company; job seeker orders (paid extras) have only user_id.
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True, nullable=True)
    buyer: Mapped[str] = mapped_column(String(10), default="employer")        # employer | seeker
    subtotal_php: Mapped[int | None] = mapped_column(Integer)                  # list price before a discount
    discount_code: Mapped[str | None] = mapped_column(String(40))
    discount_php: Mapped[int] = mapped_column(Integer, default=0)
    bill_to: Mapped[dict | None] = mapped_column(JSON, default=dict)                  # name/address/TIN snapshot for the invoice
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    product_code: Mapped[str] = mapped_column(String(40))
    product_name: Mapped[str] = mapped_column(String(100))
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    amount_php: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)   # pending | paid | failed | refunded | cancelled
    provider: Mapped[str] = mapped_column(String(20))       # sandbox | paymongo_test
    provider_ref: Mapped[str | None] = mapped_column(String(100), index=True)
    method: Mapped[str | None] = mapped_column(String(20))  # card | gcash | maya | renewal
    card_brand: Mapped[str | None] = mapped_column(String(20))
    card_last4: Mapped[str | None] = mapped_column(String(4))
    failure_reason: Mapped[str | None] = mapped_column(String(200))
    is_renewal: Mapped[bool] = mapped_column(Boolean, default=False)
    test: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    product_code: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(12), default="active")   # active | cancelled | expired | past_due
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    current_period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    current_period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False)
    renew_brand: Mapped[str | None] = mapped_column(String(20))       # saved test card for renewals
    renew_last4: Mapped[str | None] = mapped_column(String(4))
    renew_outcome: Mapped[str | None] = mapped_column(String(12))     # success | declined (which test card was saved)


class CreditEntry(Base):
    """Job credits: +1 when bought, -1 when a job post uses one."""
    __tablename__ = "credit_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    delta: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(100))
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"))
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DailyStat(Base):
    """Counters per day: visits, job views, apply clicks, applications, searches, sign-ups."""
    __tablename__ = "daily_stats"
    __table_args__ = (UniqueConstraint("day", "metric", "key", name="uq_daily_stat"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[str] = mapped_column(String(10), index=True)      # YYYY-MM-DD, Manila time
    metric: Mapped[str] = mapped_column(String(30), index=True)
    key: Mapped[str] = mapped_column(String(100), default="")
    count: Mapped[int] = mapped_column(Integer, default=0)


class DailyVisitor(Base):
    """Daily unique visitors, counted from a hash with a salt that changes every day. No raw IPs are kept."""
    __tablename__ = "daily_visitors"
    __table_args__ = (UniqueConstraint("day", "hash", name="uq_daily_visitor"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[str] = mapped_column(String(10), index=True)
    hash: Mapped[str] = mapped_column(String(32))


class ImportFeed(Base):
    """A job feed (CSV or XML) that fills a company's jobs; optionally re-read every day."""
    __tablename__ = "import_feeds"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(String(1000))
    fmt: Mapped[str] = mapped_column(String(5))              # csv | xml
    sync_daily: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_result: Mapped[dict | None] = mapped_column(JSON)


# ---------------------------------------------------------------------------
# Round 4: messages, interviews, social sign-in, discounts, phone app
# ---------------------------------------------------------------------------

class Message(Base):
    """A message in the thread attached to one application (employer team <-> applicant)."""
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), index=True)
    sender_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    side: Mapped[str] = mapped_column(String(10))            # employer | candidate | system
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))   # read by the other side


class Interview(Base):
    """An interview: the employer offers up to 3 times, the applicant picks one, both get a calendar invite."""
    __tablename__ = "interviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), index=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(12), default="proposed")   # proposed | confirmed | cancelled | declined
    mode: Mapped[str] = mapped_column(String(10), default="video")        # video | phone | onsite
    location: Mapped[str] = mapped_column(String(500), default="")        # meeting link, phone note or address
    duration_minutes: Mapped[int] = mapped_column(Integer, default=30)
    notes: Mapped[str] = mapped_column(Text, default="")
    slots: Mapped[list] = mapped_column(JSON, default=list)               # ISO datetimes offered
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    uid: Mapped[str] = mapped_column(String(80), unique=True)             # calendar UID; stays the same on updates
    sequence: Mapped[int] = mapped_column(Integer, default=0)             # calendar revision number
    cancel_reason: Mapped[str | None] = mapped_column(String(500))
    reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class OAuthIdentity(Base):
    """A Google, Facebook or LinkedIn account linked to a Sinag user."""
    __tablename__ = "oauth_identities"
    __table_args__ = (UniqueConstraint("provider", "subject", name="uq_oauth_identity"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(20))       # google | facebook | linkedin
    subject: Mapped[str] = mapped_column(String(200))       # the provider's user id
    email: Mapped[str | None] = mapped_column(String(320))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DiscountCode(Base):
    __tablename__ = "discount_codes"

    code: Mapped[str] = mapped_column(String(40), primary_key=True)       # stored upper-case
    description: Mapped[str] = mapped_column(String(200), default="")
    percent_off: Mapped[int | None] = mapped_column(Integer)              # 1-100
    amount_off_php: Mapped[int | None] = mapped_column(Integer)           # fixed pesos off
    audience: Mapped[str] = mapped_column(String(10), default="any")      # any | employer | seeker
    product_codes: Mapped[list] = mapped_column(JSON, default=list)       # empty = every product
    max_uses: Mapped[int | None] = mapped_column(Integer)
    uses: Mapped[int] = mapped_column(Integer, default=0)
    once_per_account: Mapped[bool] = mapped_column(Boolean, default=True)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DeviceToken(Base):
    """A phone running the Sinag Jobs app, for native push (Firebase Cloud Messaging)."""
    __tablename__ = "device_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token: Mapped[str] = mapped_column(String(500), unique=True)
    platform: Mapped[str] = mapped_column(String(10))       # android | ios
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
