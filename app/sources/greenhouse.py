"""Greenhouse job boards (boards.greenhouse.io/<token>). Public JSON API, no key needed."""
from __future__ import annotations

from .base import CompanySpec, Http, RawJob, parse_dt

API = "https://boards-api.greenhouse.io/v1/boards/{key}/jobs"


async def fetch(company: CompanySpec, http: Http) -> list[RawJob]:
    data = await http.get_json(API.format(key=company.key), params={"content": "true"})
    jobs: list[RawJob] = []
    for j in data.get("jobs", []):
        offices = [o.get("location") or o.get("name") for o in j.get("offices") or [] if o]
        depts = [d.get("name") for d in j.get("departments") or [] if d.get("name")]
        jobs.append(RawJob(
            external_id=str(j["id"]),
            title=j.get("title", "").strip(),
            url=j.get("absolute_url", ""),
            apply_url=j.get("absolute_url"),
            location=(j.get("location") or {}).get("name"),
            locations=[o for o in offices if o],
            department=depts[0] if depts else None,
            description_html=j.get("content"),
            posted_at=parse_dt(j.get("first_published") or j.get("updated_at")),
        ))
    return jobs
