"""Background worker.

Every CRAWL_INTERVAL_MINUTES: sync companies.yaml, crawl, then send instant job alerts.
Every minute: send queued emails, and once a day at DIGEST_HOUR (Manila time) send the daily alert digest.

Run with:  python -m app.worker
"""
from __future__ import annotations

import asyncio
import logging
import signal
import time

from .alerts import daily_digest_due, send_alerts
from .auth import cleanup_sessions
from .config import settings
from .crawler import crawl, sync_companies
from .db import SessionLocal, init_db
from .mailer import flush_outbox
from .billing import run_renewals
from .push import flush_push

log = logging.getLogger("sinag.worker")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    init_db()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:   # Windows
            pass

    next_crawl = 0.0
    while not stop.is_set():
        if time.monotonic() >= next_crawl:
            next_crawl = time.monotonic() + settings.crawl_interval_minutes * 60
            try:
                with SessionLocal() as s:
                    sync_companies(s, settings.companies_file)
                await crawl()
                send_alerts(SessionLocal, "instant")
                run_renewals(SessionLocal)
                from .importer import sync_feeds
                sync_feeds(SessionLocal)
                with SessionLocal() as s:
                    from .analytics import cleanup
                    cleanup(s)
                with SessionLocal() as s:
                    cleanup_sessions(s)
            except Exception:
                log.exception("crawl round failed; will retry next round")
        try:
            if daily_digest_due(SessionLocal):
                send_alerts(SessionLocal, "daily")
            from .interviews import send_reminders
            send_reminders(SessionLocal)
            await asyncio.to_thread(flush_outbox)
            await asyncio.to_thread(flush_push)
        except Exception:
            log.exception("email round failed; will retry in a minute")
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass
    log.info("worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
