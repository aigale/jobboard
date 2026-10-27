"""Registry of job sources. Add a new ATS by writing one async `fetch(company, http)` function."""
from __future__ import annotations

from typing import Awaitable, Callable

from . import (arbeitnow, ashby, greenhouse, himalayas, jobicy, jsonld, lever, remoteok, remotive,
               smartrecruiters, workable, weworkremotely, workingnomads)
from .base import CompanySpec, Http, RawJob, SourceError, StructuredSalary

Fetcher = Callable[[CompanySpec, Http], Awaitable[list[RawJob]]]

SOURCES: dict[str, Fetcher] = {
    "greenhouse": greenhouse.fetch,
    "lever": lever.fetch,
    "ashby": ashby.fetch,
    "workable": workable.fetch,
    "smartrecruiters": smartrecruiters.fetch,
    "jsonld": jsonld.fetch,
    # Public aggregator feeds. Unlike ATS sources these mix employers, so the crawler
    # attributes each job to the employer named by the feed (Company.source == "feed").
    "himalayas": himalayas.fetch,
    "arbeitnow": arbeitnow.fetch,
    "remoteok": remoteok.fetch,
    "jobicy": jobicy.fetch,
    "remotive": remotive.fetch,
    "workingnomads": workingnomads.fetch,
    "weworkremotely": weworkremotely.fetch,
}

FEED_SOURCES: frozenset[str] = frozenset(
    {"himalayas", "arbeitnow", "remoteok", "jobicy", "remotive", "workingnomads", "weworkremotely"}
)

__all__ = ["SOURCES", "FEED_SOURCES", "CompanySpec", "Http", "RawJob", "SourceError", "StructuredSalary"]
