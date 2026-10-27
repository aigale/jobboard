"""Command line tools.

  python -m app.cli init-db              create tables
  python -m app.cli sync-companies       load companies.yaml into the database
  python -m app.cli crawl [--company X]  crawl now (all companies, or one slug)
  python -m app.cli status               show companies, job counts and recent errors
  python -m app.cli demo                 create the demo accounts and remove previous sample listings
  python -m app.cli demo --sample-jobs   also load the offline fixtures and Easy Apply jobs (local only)
  python -m app.cli remove-demo-jobs     delete offline sample listings from an existing database
  python -m app.cli create-admin EMAIL   create a site admin (asks for a password)
  python -m app.cli outbox               show the latest emails (useful when SMTP isn't set up)
  python -m app.cli send-alerts          send the daily alert digest now
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from sqlalchemy import func, select

from .config import settings
from .crawler import crawl, sync_companies
from .db import SessionLocal, init_db
from .models import Company, CrawlRun, Job


def _print_run(run: CrawlRun) -> None:
    print(f"Crawl #{run.id}: {run.companies_ok} companies OK, {run.companies_failed} failed")
    print(f"  jobs: {run.jobs_new} new, {run.jobs_updated} updated, {run.jobs_closed} closed, "
          f"{run.jobs_skipped_not_ph} skipped (not open to the Philippines)")
    for e in run.errors or []:
        print(f"  ! {e['company']}: {e['error']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    sub.add_parser("sync-companies")
    c = sub.add_parser("crawl")
    c.add_argument("--company", help="only this company slug")
    sub.add_parser("status")
    d = sub.add_parser("demo")
    d.add_argument("--sample-jobs", action="store_true",
                   help="also load the offline fixture jobs and three Easy Apply listings")
    sub.add_parser("remove-demo-jobs")
    ca = sub.add_parser("create-admin")
    ca.add_argument("email")
    ca.add_argument("--name", default="Site admin")
    ob = sub.add_parser("outbox")
    ob.add_argument("-n", type=int, default=5)
    sub.add_parser("send-alerts")
    sub.add_parser("vapid-keys")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    init_db()
    if args.cmd == "init-db":
        print(f"Database ready: {settings.database_url}")
    elif args.cmd == "sync-companies":
        with SessionLocal() as s:
            created, updated = sync_companies(s, settings.companies_file)
        print(f"Companies: {created} added, {updated} updated from {settings.companies_file}")
    elif args.cmd == "crawl":
        with SessionLocal() as s:
            sync_companies(s, settings.companies_file)
        _print_run(asyncio.run(crawl(only_slug=args.company)))
    elif args.cmd == "status":
        with SessionLocal() as s:
            rows = s.execute(select(Company, func.count(Job.id))
                             .outerjoin(Job, (Job.company_id == Company.id) & (Job.status == "open"))
                             .group_by(Company.id).order_by(Company.name)).all()
            print(f"{'company':32} {'source':16} {'open':>5}  last crawl")
            for comp, n in rows:
                when = comp.last_crawled_at.strftime("%Y-%m-%d %H:%M") if comp.last_crawled_at else "never"
                flag = f"  ERROR: {comp.last_error[:80]}" if comp.last_status == "error" else ""
                print(f"{comp.name[:32]:32} {comp.source:16} {n:>5}  {when}{flag}")
            total = s.scalar(select(func.count()).select_from(Job).where(Job.status == "open"))
            print(f"\n{total} open jobs")
    elif args.cmd == "demo":
        from .demo import load_demo, load_demo_accounts, remove_demo_jobs
        if args.sample_jobs:
            _print_run(load_demo())
        else:
            removed = remove_demo_jobs()
            if removed:
                print(f"Removed {removed} demo job(s) from the database.")
        print()
        print("Demo accounts (local use only; password for all three: sinag-demo-123):")
        for line in load_demo_accounts(sample_jobs=args.sample_jobs):
            print("  " + line)
    elif args.cmd == "remove-demo-jobs":
        from .demo import remove_demo_jobs
        removed = remove_demo_jobs()
        print(f"Removed {removed} demo job(s) and the offline sample companies.")
    elif args.cmd == "create-admin":
        import getpass
        from .auth import _check_email, _check_password, hash_password
        from .models import User
        email = _check_email(args.email)
        pw = getpass.getpass("Password (at least 8 characters): ")
        _check_password(pw)
        if getpass.getpass("Repeat it: ") != pw:
            print("The passwords don't match.")
            return 1
        with SessionLocal() as s:
            u = s.scalar(select(User).where(User.email == email))
            if u is None:
                u = User(email=email, name=args.name, role="admin", password_hash=hash_password(pw))
                s.add(u)
            else:
                u.role, u.password_hash = "admin", hash_password(pw)
            from .models import utcnow
            u.email_verified_at = u.email_verified_at or utcnow()
            s.commit()
        print(f"Admin ready: {email}. Sign in on the site, then open /admin.html")
    elif args.cmd == "outbox":
        from .models import OutboxEmail
        with SessionLocal() as s:
            for m in s.scalars(select(OutboxEmail).order_by(OutboxEmail.id.desc()).limit(args.n)):
                state = "sent" if m.sent_at and not m.error else (m.error or "waiting")
                print(f"#{m.id} to {m.to} [{state}]\nSubject: {m.subject}\n\n{m.body}\n" + "-" * 60)
    elif args.cmd == "vapid-keys":
        from .push import generate_keys
        pub, priv = generate_keys()
        print("Add these lines to .env, then restart (docker compose up -d):\n")
        print(f"VAPID_PUBLIC_KEY={pub}\nVAPID_PRIVATE_KEY={priv}\nVAPID_SUBJECT=mailto:you@yourdomain.ph")
        print("\nKeep the private key secret. Changing the keys later turns push off for everyone until they turn it on again.")
    elif args.cmd == "send-alerts":
        from .alerts import send_alerts
        from .mailer import flush_outbox
        n = send_alerts(SessionLocal, "daily")
        flush_outbox()
        print(f"{n} alert emails queued and sent (or logged if SMTP_HOST is empty).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
