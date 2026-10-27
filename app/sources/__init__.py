"""Registry of job sources. Add a new ATS by writing one async `fetch(company, http)` function."""
from __future__ import annotations

from typing import Awaitable, Callable

from . import ashby, greenhouse, jsonld, lever, smartrecruiters, workable
from .base import CompanySpec, Http, RawJob, SourceError, StructuredSalary

Fetcher = Callable[[CompanySpec, Http], Awaitable[list[RawJob]]]

SOURCES: dict[str, Fetcher] = {
    "greenhouse": greenhouse.fetch,
    "lever": lever.fetch,
    "ashby": ashby.fetch,
    "workable": workable.fetch,
    "smartrecruiters": smartrecruiters.fetch,
    "jsonld": jsonld.fetch,
}

__all__ = ["SOURCES", "CompanySpec", "Http", "RawJob", "SourceError", "StructuredSalary"]
