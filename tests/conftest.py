"""Storage-backed tests use SQLite by default. Set TEST_DATABASE_URL=postgresql://... to run
them against Postgres too (tables are dropped before each test)."""
import os

import pytest

PG = os.environ.get("TEST_DATABASE_URL", "")


@pytest.fixture
def db_url():
    if PG:
        import psycopg
        with psycopg.connect(PG, autocommit=True) as c:
            c.execute("DROP TABLE IF EXISTS readings, events, demo_subscribers, sms_log")
    return PG
