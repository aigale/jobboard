import dataclasses
import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.db import init_db, make_engine  # noqa: E402

# Billing is switched off for the older tests (they post many jobs); billing tests switch it on.
object.__setattr__(settings, "billing_enabled", False)


@pytest.fixture
def cfg():
    return dataclasses.replace(settings, per_host_delay_seconds=0.0, close_after_missed_crawls=2)


TEST_DB = os.getenv("TEST_DATABASE_URL")   # e.g. postgresql+psycopg://user@host/db to run on Postgres


def fresh_engine(tmp_path):
    if TEST_DB:
        engine = make_engine(TEST_DB)
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
        return engine
    return make_engine(f"sqlite:///{tmp_path / 'test.db'}")


@pytest.fixture
def factory(tmp_path):
    engine = fresh_engine(tmp_path)
    init_db(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()          # close pooled connections so long Postgres runs don't run out
