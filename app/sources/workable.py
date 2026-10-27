"""Workable career pages (apply.workable.com/<account>). Public widget API, no key needed."""
from __future__ import annotations

from .base import CompanySpec, Http, RawJob, parse_dt

API = "https://apply.workable.com/api/v1/widget/accounts/{key}"


async def fetch(company: CompanySpec, http: Http) -> list[RawJob]:
    data = await http.get_json(API.format(key=company.key), params={"details": "true"})
    jobs: list[RawJob] = []
    for j in data.get("jobs", []):
        locs = []
        codes = []
        for loc in j.get("locations") or []:
            locs.append(", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x))
            if loc.get("countryCode"):
                codes.append(loc["countryCode"])
        main = ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x)
        jobs.append(RawJob(
            external_id=j.get("shortcode") or j.get("code") or j.get("url"),
            title=(j.get("title") or "").strip(),
            url=j.get("url") or j.get("shortlink") or "",
            apply_url=j.get("application_url"),
            location=main or None,
            locations=[x for x in locs if x],
            country_codes=codes,
            remote=bool(j.get("telecommuting")),
            workplace_type="remote" if j.get("telecommuting") else None,
            department=j.get("department") or None,
            employment_type=j.get("employment_type") or None,
            description_html=j.get("description"),
            posted_at=parse_dt(j.get("published_on") or j.get("created_at")),
        ))
    return jobs
