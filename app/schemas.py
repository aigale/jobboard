"""Shapes returned by the API."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class CompanyOut(BaseModel):
    name: str
    slug: str
    logo_url: str | None = None
    verified: bool = True


class JobSummary(BaseModel):
    id: int
    title: str
    company: CompanyOut
    location: str
    city: str | None
    setup: str
    shift: str
    level: str
    category: str
    perks: list[str]
    salary_min: int | None = Field(None, description="PHP per month")
    salary_max: int | None = Field(None, description="PHP per month")
    salary_original: str | None = None
    posted_at: datetime
    featured: bool
    url: str
    job_type: str = "Not stated"
    industry: str | None = None
    easy_apply: bool = False


class JobDetail(JobSummary):
    apply_url: str | None
    location_raw: str | None
    department: str | None
    employment_type: str | None
    description_html: str | None
    status: str
    first_seen_at: datetime


class JobPage(BaseModel):
    total: int
    page: int
    page_size: int
    jobs: list[JobSummary]


class FacetCount(BaseModel):
    value: str
    count: int


class Facets(BaseModel):
    total_open: int
    companies: int
    last_crawl_at: datetime | None
    location: list[FacetCount]
    category: list[FacetCount]
    setup: list[FacetCount]
    shift: list[FacetCount]
    level: list[FacetCount]
    perk: list[FacetCount]
    job_type: list[FacetCount] = []
    industry: list[FacetCount] = []
    easy_apply: int = 0


class AlertIn(BaseModel):
    email: str
    category: str | None = None
    location: str | None = None

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        import re
        v = v.strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v) or len(v) > 320:
            raise ValueError("Enter a valid email address, like juan@example.com")
        return v

    @field_validator("category", "location")
    @classmethod
    def _blank(cls, v: str | None) -> str | None:
        return v.strip() or None if v else None
