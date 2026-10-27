"""Rules shared by the public board, seeker, employer and alert code."""
from __future__ import annotations

import html
import re
from datetime import datetime

import nh3
from sqlalchemy import and_, or_

from .models import Application, Company, Job, aware, utcnow

LOCATIONS = ["Remote (PH-based)", "Metro Manila", "Cebu", "Davao", "Iloilo", "Bacolod", "Clark, Pampanga", "Baguio",
             "Cagayan de Oro", "Laguna", "Cavite", "Batangas", "Bulacan", "Dumaguete", "General Santos",
             "Zamboanga", "Tacloban", "Bicol", "Philippines (other)"]
SETUPS = ["Remote", "Hybrid", "On-site"]
SHIFTS = ["Day shift", "Night shift", "Rotating", "Not stated"]
LEVELS = ["Entry level", "Mid level", "Senior"]
JOB_TYPES = ["Full-time", "Part-time", "Contract", "Freelance", "Internship"]
CATEGORIES = ["Customer Support", "Data", "Design", "Education", "Engineering", "Finance", "Healthcare",
              "HR & Recruiting", "Legal", "Marketing", "Operations", "Sales", "Virtual Assistant", "Other"]
INDUSTRIES = ["BPO & Outsourcing", "Banking & Finance", "Construction & Real Estate", "Education",
              "E-commerce & Retail", "Energy & Utilities", "Food & Hospitality", "Government & Nonprofit",
              "Healthcare", "Logistics & Transport", "Manufacturing", "Media & Marketing",
              "Software & Technology", "Telecommunications", "Other"]
PERKS = ["HMO day 1", "HMO", "13th month", "Night diff", "Paid leave", "Rice allowance", "Internet allowance",
         "Equipment provided", "Equity", "Commission", "Bonus", "Free shuttle", "Free meals", "Learning budget"]

STAGES = ["applied", "screening", "shortlisted", "interview", "offer", "hired", "rejected"]
STAGE_LABELS = {"applied": "New", "screening": "Screening", "shortlisted": "Shortlisted", "interview": "Interview",
                "offer": "Offer", "hired": "Hired", "rejected": "Rejected", "withdrawn": "Withdrawn"}
# Stage changes the candidate is told about (in the app and by email)
NOTIFY_STAGES = {"shortlisted", "interview", "offer", "hired", "rejected"}


def public_job_filter(now: datetime | None = None):
    """Conditions for a job to appear on the public board."""
    now = now or utcnow()
    return and_(
        Job.status == "open", Company.active.is_(True), Company.approval == "approved",
        or_(Job.publish_at.is_(None), Job.publish_at <= now),
        or_(Job.expires_at.is_(None), Job.expires_at > now),
    )


def is_public(job: Job, now: datetime | None = None) -> bool:
    now = now or utcnow()
    c = job.company
    return (job.status == "open" and c.active and c.approval == "approved"
            and (job.publish_at is None or aware(job.publish_at) <= now)
            and (job.expires_at is None or aware(job.expires_at) > now))


def employer_job_state(job: Job, now: datetime | None = None) -> str:
    now = now or utcnow()
    if job.status == "draft":
        return "Draft"
    if job.status == "closed":
        return "Closed"
    if job.expires_at is not None and aware(job.expires_at) <= now:
        return "Expired"
    if job.publish_at is not None and aware(job.publish_at) > now:
        return "Scheduled"
    if job.company.approval != "approved":
        return "Waiting for approval"
    return "Live"


def candidate_status(a: Application) -> str:
    if a.stage == "withdrawn":
        return "Withdrawn"
    if a.stage == "rejected":
        return "Not selected"
    if a.stage == "applied":
        return "Viewed" if a.viewed_at else "Submitted"
    return {"screening": "Under review"}.get(a.stage, STAGE_LABELS.get(a.stage, a.stage.title()))


def text_to_html(text: str) -> str:
    """Plain text from a form -> safe HTML. Blank lines split paragraphs; '- ' lines become bullets."""
    blocks = re.split(r"\n\s*\n", (text or "").strip())
    out = []
    for block in blocks:
        lines = [l.rstrip() for l in block.splitlines() if l.strip()]
        if lines and all(re.match(r"\s*[-•*]\s+", l) for l in lines):
            items = "".join(f"<li>{html.escape(re.sub(r'^\s*[-•*]\s+', '', l))}</li>" for l in lines)
            out.append(f"<ul>{items}</ul>")
        elif lines:
            out.append("<p>" + "<br>".join(html.escape(l) for l in lines) + "</p>")
    return nh3.clean("".join(out), tags={"p", "br", "ul", "li"})


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:80] or "job"


def job_path(job) -> str:
    return f"/jobs/{job.id}/{slug(job.title)}"


def is_featured(job, now: datetime | None = None) -> bool:
    now = now or utcnow()
    return bool(job.featured or (job.featured_until is not None and aware(job.featured_until) > now))


def normalize_job_type(raw: str | None, title: str = "") -> str:
    r = (raw or "").lower()
    both = f"{r} {title.lower()}"
    if re.search(r"\bintern(ship)?\b|\bojt\b", both):
        return "Internship"
    if re.search(r"part[\s-]?time", both):
        return "Part-time"
    if re.search(r"freelanc", r):
        return "Freelance"
    if re.search(r"contract|temporary|\btemp\b|fixed[\s-]term|project[\s-]based", r):
        return "Contract"
    if re.search(r"full[\s-]?time|fulltime|permanent|regular", r):
        return "Full-time"
    return "Not stated"
