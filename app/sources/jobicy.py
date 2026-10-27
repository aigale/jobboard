"""Jobicy remote job board (jobicy.com). Public JSON API, no key needed.

`geo=philippines` returns roles open to the Philippines plus APAC/Anywhere roles.
Use `options: {geo: ..., count: ...}` to change the slice.
"""
from __future__ import annotations

from .base import CompanySpec, Http, RawJob, StructuredSalary, parse_dt, salary_interval

API = "https://jobicy.com/api/v2/remote-jobs"


def _salary(job: dict) -> StructuredSalary | None:
    lo = job.get("salaryMin") or job.get("annualSalaryMin")
    hi = job.get("salaryMax") or job.get("annualSalaryMax")
    if not lo and not hi:
        return None
    return StructuredSalary(job.get("salaryCurrency") or "USD", lo or None, hi or None,
                            salary_interval(job.get("salaryPeriod")) or "YEAR")


async def fetch(company: CompanySpec, http: Http) -> list[RawJob]:
    params = {
        "count": min(int(company.options.get("count", 100)), 100),
        "geo": str(company.options.get("geo", "philippines")),
    }
    data = await http.get_json(API, params=params)
    jobs: list[RawJob] = []
    for j in data.get("jobs") or []:
        geo = (j.get("jobGeo") or "").strip()
        industries = j.get("jobIndustry") or []
        if isinstance(industries, str):
            industries = [industries]
        types = j.get("jobType") or []
        if isinstance(types, str):
            types = [types]
        low = geo.lower()
        jobs.append(RawJob(
            external_id=str(j.get("id") or j.get("jobSlug") or j.get("url") or ""),
            title=j.get("jobTitle") or "",
            url=j.get("url") or "",
            apply_url=j.get("url"),
            company_name=(j.get("companyName") or "").strip() or None,
            company_logo=j.get("companyLogo"),
            location=geo or None,
            locations=[geo] if geo else [],
            remote=True,
            workplace_type="remote",
            department=", ".join(str(x) for x in industries) or None,
            employment_type=", ".join(str(x) for x in types) or None,
            description_html=j.get("jobDescription"),
            posted_at=parse_dt(j.get("pubDate")),
            salary=_salary(j),
            worldwide=(not geo or "anywhere" in low or "worldwide" in low),
        ))
    return [job for job in jobs if job.external_id and job.title and job.url]
