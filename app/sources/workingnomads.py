"""Working Nomads (workingnomads.com). Public JSON feed, no key needed."""
from __future__ import annotations

import re

from .base import CompanySpec, Http, RawJob, parse_dt

API = "https://www.workingnomads.com/api/exposed_jobs/"
_ID = re.compile(r"/job/go/(\d+)/?")


async def fetch(company: CompanySpec, http: Http) -> list[RawJob]:
    data = await http.get_json(API)
    jobs: list[RawJob] = []
    for j in data if isinstance(data, list) else []:
        url = j.get("url") or ""
        match = _ID.search(url)
        location = (j.get("location") or "").strip()
        low = location.lower()
        tags = (j.get("tags") or "").strip()
        department = j.get("category_name") or tags or None
        jobs.append(RawJob(
            external_id=match.group(1) if match else url,
            title=j.get("title") or "",
            url=url,
            apply_url=url,
            company_name=(j.get("company_name") or "").strip() or None,
            location=location or None,
            locations=[location] if location else [],
            remote=True,
            workplace_type="remote",
            department=department,
            description_html=j.get("description"),
            posted_at=parse_dt(j.get("pub_date")),
            worldwide=(not location or "worldwide" in low or "anywhere" in low),
        ))
    return [job for job in jobs if job.external_id and job.title and job.url]
