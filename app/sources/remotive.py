"""Remotive remote job board (remotive.com). Public JSON API, no key needed.

The public endpoint returns the latest jobs (currently 10) and free-text salary
strings, which we pass through for the normalizer to parse.
"""
from __future__ import annotations

from .base import CompanySpec, Http, RawJob, parse_dt

API = "https://remotive.com/api/remote-jobs"


async def fetch(company: CompanySpec, http: Http) -> list[RawJob]:
    data = await http.get_json(API)
    jobs: list[RawJob] = []
    for j in data.get("jobs") or []:
        location = (j.get("candidate_required_location") or "").strip()
        low = location.lower()
        jobs.append(RawJob(
            external_id=str(j.get("id") or j.get("url") or ""),
            title=j.get("title") or "",
            url=j.get("url") or "",
            apply_url=j.get("url"),
            company_name=(j.get("company_name") or "").strip() or None,
            company_logo=j.get("company_logo"),
            location=location or None,
            locations=[location] if location else [],
            remote=True,
            workplace_type="remote",
            department=j.get("category"),
            employment_type=j.get("job_type"),
            description_html=j.get("description"),
            posted_at=parse_dt(j.get("publication_date")),
            salary_text=(j.get("salary") or "").strip() or None,
            worldwide=(not location or "worldwide" in low or "anywhere" in low or "global" in low),
        ))
    return [job for job in jobs if job.external_id and job.title and job.url]
