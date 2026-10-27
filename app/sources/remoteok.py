"""Remote OK (remoteok.com). Public JSON feed, no key needed."""
from __future__ import annotations

from .base import CompanySpec, Http, RawJob, StructuredSalary, fix_mojibake, parse_dt

API = "https://remoteok.com/api"


async def fetch(company: CompanySpec, http: Http) -> list[RawJob]:
    data = await http.get_json(API)
    jobs: list[RawJob] = []
    for j in data if isinstance(data, list) else []:
        # The first array entry is Remote OK's legal notice, not a job.
        if not isinstance(j, dict) or not j.get("position") or not j.get("id"):
            continue
        location = (j.get("location") or "").strip()
        lo, hi = j.get("salary_min") or 0, j.get("salary_max") or 0
        jobs.append(RawJob(
            external_id=str(j["id"]),
            title=fix_mojibake(j.get("position")) or "",
            url=j.get("url") or j.get("apply_url") or "",
            apply_url=j.get("apply_url") or j.get("url"),
            company_name=fix_mojibake(j.get("company")) or None,
            company_logo=j.get("company_logo") or j.get("logo"),
            location=location or None,
            locations=[location] if location else [],
            remote=True,
            workplace_type="remote",
            department=", ".join(str(x) for x in (j.get("tags") or []) if x) or None,
            description_html=fix_mojibake(j.get("description")),
            posted_at=parse_dt(j.get("date")),
            salary=StructuredSalary("USD", lo or None, hi or None, "YEAR") if (lo or hi) else None,
            worldwide=(not location or "worldwide" in location.lower() or "anywhere" in location.lower()),
        ))
    return [job for job in jobs if job.title and job.url]
