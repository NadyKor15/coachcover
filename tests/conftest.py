"""Database fixtures. DB tests use the local test Postgres from scripts/test-db-up.sh.

A missing database fails the DB tests loudly instead of skipping them. Run
only the pure tests with: pytest -m "not db".
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

TEST_DSN = (
    os.environ.get("COACHCOVER_TEST_DSN")
    or os.environ.get("COVERAGE_TEST_DSN")  # old name, still honoured
    or "postgresql://postgres@127.0.0.1:55432/postgres"
)


def masked_dsn(dsn: str) -> str:
    """The DSN with any password replaced, safe to print."""
    try:
        parts = conninfo_to_dict(dsn)
    except psycopg.ProgrammingError:
        return "<unparsable DSN>"
    if parts.get("password"):
        parts["password"] = "***"
    return make_conninfo(**parts)


@pytest.fixture(scope="session")
def pg_admin():
    try:
        conn = psycopg.connect(TEST_DSN, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as exc:
        pytest.fail(
            f"test Postgres not reachable at {masked_dsn(TEST_DSN)}; start it: scripts/test-db-up.sh"
            f" ({type(exc).__name__})",
            pytrace=False,
        )
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def new_database(pg_admin):
    """Factory: an empty database per call, dropped at the end of the session."""
    created: list[str] = []
    conns: list[psycopg.Connection] = []

    def make() -> psycopg.Connection:
        name = f"cov_test_{uuid.uuid4().hex[:12]}"
        pg_admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        created.append(name)
        conn = psycopg.connect(make_conninfo(TEST_DSN, dbname=name), autocommit=True)
        conns.append(conn)
        return conn

    yield make
    for conn in conns:
        conn.close()
    for name in created:
        pg_admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture(scope="session")
def school(new_database):
    """One database with the training school, shared by read-only tests."""
    from coachcover.seed import load_school

    conn = new_database()
    load_school(conn)
    return conn


@pytest.fixture(scope="session")
def deck_school(new_database):
    """The deck story of the training school (judge scenario network_loan: Marina's 06:00 deck shift), read-only."""
    from coachcover.seed import load_school

    conn = new_database()
    load_school(conn, story="deck")
    return conn
