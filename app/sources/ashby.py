"""Ashby job boards (jobs.ashbyhq.com/<name>). Public JSON API, no key needed."""
from __future__ import annotations

import re

from .base import CompanySpec, Http, RawJob, StructuredSalary, parse_dt

API = "https://api.ashbyhq.com/posting-api/job-board/{key}"


def _salary(comp: dict | None) -> StructuredSalary | None:
    for c in (comp or {}).get("summaryComponents") or []:
        if c.get("compensationType") == "Salary" and c.get("currencyCode"):
            m = re.search(r"(HOUR|DAY|WEEK|MONTH|YEAR)", (c.get("interval") or "").upper())
            return StructuredSalary(c["currencyCode"], c.get("minValue"), c.get("maxValue"), m.group(1) if m else "YEAR")
    return None


async def fetch(company: CompanySpec, http: Http) -> list[RawJob]:
    data = await http.get_json(API.format(key=company.key), params={"includeCompensation": "true"})
    jobs: list[RawJob] = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        addr = ((j.get("address") or {}).get("postalAddress") or {})
        secondary = [s.get("location") for s in j.get("secondaryLocations") or [] if s.get("location")]
        countries = [addr.get("addressCountry")] + [
            ((s.get("address") or {}).get("postalAddress") or {}).get("addressCountry")
            for s in j.get("secondaryLocations") or []]
        wt = (j.get("workplaceType") or "").lower().replace("onsite", "onsite")
        jobs.append(RawJob(
            external_id=str(j.get("id") or j.get("jobUrl")),
            title=(j.get("title") or "").strip(),
            url=j.get("jobUrl", ""),
            apply_url=j.get("applyUrl"),
            location=j.get("location"),
            locations=secondary + [c for c in countries if c],
            remote=j.get("isRemote"),
            workplace_type=wt or None,
            department=j.get("department") or j.get("team"),
            employment_type=j.get("employmentType"),
            description_html=j.get("descriptionHtml"),
            description_text=j.get("descriptionPlain"),
            posted_at=parse_dt(j.get("publishedAt")),
            salary=_salary(j.get("compensation")),
        ))
    return jobs
