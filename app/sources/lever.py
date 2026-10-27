"""Lever job sites (jobs.lever.co/<company>). Public JSON API, no key needed.

Set `options: {region: eu}` in companies.yaml for companies hosted on jobs.eu.lever.co.
"""
from __future__ import annotations

from .base import CompanySpec, Http, RawJob, StructuredSalary, parse_dt

API = {"global": "https://api.lever.co/v0/postings/{key}", "eu": "https://api.eu.lever.co/v0/postings/{key}"}
_INTERVALS = {"per-year-salary": "YEAR", "per-month-salary": "MONTH", "per-week-salary": "WEEK",
              "per-day-wage": "DAY", "per-hour-wage": "HOUR"}


async def fetch(company: CompanySpec, http: Http) -> list[RawJob]:
    url = API.get(company.options.get("region", "global"), API["global"]).format(key=company.key)
    data = await http.get_json(url, params={"mode": "json"})
    jobs: list[RawJob] = []
    for p in data if isinstance(data, list) else []:
        cats = p.get("categories") or {}
        parts = [p.get("description") or ""]
        for section in p.get("lists") or []:
            parts.append(f"<h3>{section.get('text', '')}</h3><ul>{section.get('content', '')}</ul>")
        parts.append(p.get("additional") or "")
        sr = p.get("salaryRange") or {}
        salary = None
        if sr.get("currency") and (sr.get("min") or sr.get("max")):
            salary = StructuredSalary(sr["currency"], sr.get("min"), sr.get("max"), _INTERVALS.get(sr.get("interval"), "YEAR"))
        jobs.append(RawJob(
            external_id=p["id"],
            title=(p.get("text") or "").strip(),
            url=p.get("hostedUrl", ""),
            apply_url=p.get("applyUrl"),
            location=cats.get("location"),
            locations=[x for x in cats.get("allLocations") or [] if x],
            country_codes=[p["country"]] if p.get("country") else [],
            workplace_type=p.get("workplaceType") if p.get("workplaceType") not in (None, "unspecified") else None,
            department=cats.get("team") or cats.get("department"),
            employment_type=cats.get("commitment"),
            description_html="".join(parts),
            posted_at=parse_dt(p.get("createdAt")),
            salary=salary,
        ))
    return jobs
