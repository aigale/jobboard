"""Shared types and the polite HTTP client every source uses."""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx


@dataclass
class StructuredSalary:
    currency: str
    min: float | None
    max: float | None
    interval: str | None   # HOUR | DAY | WEEK | MONTH | YEAR


@dataclass
class RawJob:
    """A job exactly as one source describes it, before any Philippine-specific cleanup."""
    external_id: str
    title: str
    url: str
    apply_url: str | None = None
    location: str | None = None
    locations: list[str] = field(default_factory=list)
    country_codes: list[str] = field(default_factory=list)
    remote: bool | None = None
    workplace_type: str | None = None          # remote | hybrid | onsite, when the source says so
    department: str | None = None
    employment_type: str | None = None
    description_html: str | None = None
    description_text: str | None = None
    posted_at: datetime | None = None
    salary: StructuredSalary | None = None
    salary_text: str | None = None
    # Aggregator feeds mix employers: the source says who is hiring and (when the feed
    # states it) that the role is open anywhere. Direct ATS sources leave these empty.
    company_name: str | None = None
    company_logo: str | None = None
    worldwide: bool = False


@dataclass
class CompanySpec:
    slug: str
    name: str
    source: str
    key: str
    careers_url: str | None = None
    options: dict[str, Any] = field(default_factory=dict)


class SourceError(Exception):
    """The source could not be read. The company's existing jobs are left untouched."""


class Http:
    """httpx wrapper: identifies itself, waits between requests to the same host,
    retries on 429/5xx with backoff, and checks robots.txt for plain web pages."""

    def __init__(self, client: httpx.AsyncClient, per_host_delay: float = 1.0, max_retries: int = 3):
        self.client = client
        self.per_host_delay = per_host_delay
        self.max_retries = max_retries
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._host_last: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}

    async def _wait_turn(self, host: str) -> asyncio.Lock:
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        await lock.acquire()
        gap = time.monotonic() - self._host_last.get(host, 0.0)
        if gap < self.per_host_delay:
            await asyncio.sleep(self.per_host_delay - gap)
        return lock

    async def get(self, url: str, *, params: dict | None = None, check_robots: bool = False) -> httpx.Response:
        if check_robots and not await self.allowed(url):
            raise SourceError(f"robots.txt disallows {url}")
        host = urlsplit(url).netloc
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            lock = await self._wait_turn(host)
            try:
                resp = await self.client.get(url, params=params, follow_redirects=True)
            except httpx.HTTPError as exc:
                last_exc = exc
                resp = None
            finally:
                self._host_last[host] = time.monotonic()
                lock.release()
            if resp is not None:
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_exc = SourceError(f"HTTP {resp.status_code} from {url}")
                    retry_after = resp.headers.get("retry-after", "")
                    delay = float(retry_after) if retry_after.isdigit() else 2 ** attempt + random.random()
                    if attempt < self.max_retries:
                        await asyncio.sleep(min(delay, 60))
                        continue
                    break
                if resp.status_code == 404:
                    raise SourceError(f"Not found (404): {url}. Check the board name in companies.yaml.")
                if resp.status_code >= 400:
                    raise SourceError(f"HTTP {resp.status_code} from {url}")
                return resp
            if attempt < self.max_retries:
                await asyncio.sleep(2 ** attempt + random.random())
        raise SourceError(f"Gave up on {url}: {last_exc}")

    async def get_json(self, url: str, **kw) -> Any:
        resp = await self.get(url, **kw)
        try:
            return resp.json()
        except ValueError as exc:
            raise SourceError(f"Expected JSON from {url}") from exc

    async def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base not in self._robots:
            parser: RobotFileParser | None = RobotFileParser()
            try:
                resp = await self.client.get(base + "/robots.txt", follow_redirects=True)
                if resp.status_code >= 400:
                    parser = None            # no robots.txt: everything allowed
                else:
                    parser.parse(resp.text.splitlines())
            except httpx.HTTPError:
                parser = None
            self._robots[base] = parser
        parser = self._robots[base]
        if parser is None:
            return True
        ua = self.client.headers.get("user-agent", "*")
        return parser.can_fetch(ua, url)


def fix_mojibake(text: str | None) -> str | None:
    """Repair UTF-8 text that a feed decoded as Latin-1 (notably the Remote OK API)."""
    if not text or not any(marker in text for marker in ("â", "Â", "Ã", "ð")):
        return text
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def salary_interval(value: Any) -> str | None:
    """Map a feed's free-text pay period to HOUR | DAY | WEEK | MONTH | YEAR."""
    text = str(value or "").strip().lower()
    for word, interval in (("hour", "HOUR"), ("day", "DAY"), ("week", "WEEK"),
                           ("month", "MONTH"), ("year", "YEAR"), ("annual", "YEAR")):
        if word in text:
            return interval
    return None


def parse_dt(value: Any) -> datetime | None:
    """Accepts ISO strings, YYYY-MM-DD, or epoch seconds/milliseconds."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        ts = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    s = str(value).strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
