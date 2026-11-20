"""Test setup.

Database tests run against a throwaway database (default: `skeddie_test` on the
same server as DATABASE_URL; override with TEST_DATABASE_URL). At the start of
the run it is dropped, rebuilt from packages/db/migrations in order, and loaded
with seed.py. Each test then runs inside a transaction that is rolled back, so
tests never see each other's data.

Pure tests (matchups, solver on hand-built problems) need no database.
"""

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

SOLVER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SOLVER_DIR.parents[1]
MIGRATIONS_DIR = REPO_ROOT / "packages" / "db" / "migrations"

load_dotenv(REPO_ROOT / ".env")


def _test_database_url() -> str | None:
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        return url
    base = os.environ.get("DATABASE_URL")
    return f"{base.rsplit('/', 1)[0]}/skeddie_test" if base else None


TEST_DATABASE_URL = _test_database_url()
TEST_DATABASE_NAME = TEST_DATABASE_URL.rsplit("/", 1)[1].split("?")[0] if TEST_DATABASE_URL else None

if TEST_DATABASE_URL:
    # The test database is dropped and recreated; never let that be a real one.
    if not TEST_DATABASE_NAME.endswith("_test"):
        raise RuntimeError(f"Refusing to use {TEST_DATABASE_NAME!r} for tests: the name must end with '_test'")
    # Point the application's engine at the test database before it is imported.
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL


def _libpq_url(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


@pytest.fixture(scope="session")
def database_ready():
    if not TEST_DATABASE_URL:
        pytest.skip("Set DATABASE_URL or TEST_DATABASE_URL to run database tests")

    import psycopg
    from psycopg import sql

    plain = _libpq_url(TEST_DATABASE_URL)
    maintenance = f"{plain.rsplit('/', 1)[0]}/postgres"
    with psycopg.connect(maintenance, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(TEST_DATABASE_NAME)))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEST_DATABASE_NAME)))

    with psycopg.connect(plain, autocommit=True) as conn:
        for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
            conn.execute(migration.read_text(encoding="utf-8"))

    from seed import seed_database

    seed_database()
    yield


@pytest.fixture
def db(database_ready):
    """A Session inside a transaction that is rolled back after the test.

    commit() / rollback() inside the code under test only release / roll back
    a savepoint, so the outer transaction always discards everything.
    """
    from sqlalchemy.orm import Session

    from database import engine

    connection = engine.connect()
    outer = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        outer.rollback()
        connection.close()
