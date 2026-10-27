"""Himalayas remote job board (himalayas.app). Public JSON API, no key needed."""
from __future__ import annotations

from .base import CompanySpec, Http, RawJob, StructuredSalary, parse_dt, salary_interval

API = "https://himalayas.app/jobs/api"
_WORLDWIDE = ("worldwide", "anywhere", "global", "remote")


def _salary(job: dict) -> StructuredSalary | None:
    lo, hi = job.get("minSalary"), job.get("maxSalary")
    if not lo and not hi:
        return None
    return StructuredSalary(job.get("currency") or "USD", lo or None, hi or None,
                            salary_interval(job.get("salaryPeriod")) or "YEAR")


def _worldwide(locations: list[str]) -> bool:
    if not locations:
        return True
    return any(any(word in location.lower() for word in _WORLDWIDE) for location in locations)


async def fetch(company: CompanySpec, http: Http) -> list[RawJob]:
    max_pages = max(1, int(company.options.get("max_pages", 5)))
    jobs: list[RawJob] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()

    for _ in range(max_pages):
        params: dict[str, str | int] = {"limit": 20}
        if cursor:
            params["cursor"] = cursor
        data = await http.get_json(API, params=params)
        batch = data.get("jobs") or []
        for j in batch:
            locations = [str(x).strip() for x in (j.get("locationRestrictions") or []) if str(x).strip()]
            categories = [str(x) for x in (j.get("parentCategories") or j.get("categories") or []) if x]
            jobs.append(RawJob(
                external_id=str(j.get("guid") or j.get("applicationLink") or ""),
                title=j.get("title") or "",
                url=j.get("guid") or j.get("applicationLink") or "",
                apply_url=j.get("applicationLink"),
                company_name=(j.get("companyName") or "").strip() or None,
                company_logo=j.get("companyLogo"),
                location=", ".join(locations) or None,
                locations=locations,
                remote=True,
                workplace_type="remote",
                department=", ".join(categories) or None,
                employment_type=j.get("employmentType"),
                description_html=j.get("description"),
                posted_at=parse_dt(j.get("pubDate")),
                salary=_salary(j),
                worldwide=_worldwide(locations),
            ))
        cursor = data.get("nextCursor")
        if len(batch) < 20 or not cursor or cursor in seen_cursors:
            break
        seen_cursors.add(cursor)
    return [job for job in jobs if job.external_id and job.title and job.url]
