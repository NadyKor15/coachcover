"""The extension lane's own schema track: `coachcover/ext_migrations/*.sql`, applied on top of the core schema.

Kept apart from `coachcover/migrations/` so the core migration list stays as it is. Every extension entry
point calls `ensure_schema` (one cheap query when everything is applied), so the tables come back by
themselves after a demo reset drops the public schema.
"""

from __future__ import annotations

from pathlib import Path

import psycopg

EXT_MIGRATIONS_DIR = Path(__file__).parent / "ext_migrations"
_LOCK_KEY = 0x65787430


def _versions() -> list[Path]:
    return sorted(EXT_MIGRATIONS_DIR.glob("*.sql"))


def ensure_schema(conn: psycopg.Connection) -> list[str]:
    """Apply pending extension migrations in file-name order. Returns the versions applied now."""
    files = _versions()
    if conn.execute("SELECT to_regclass('ext_schema_migrations')").fetchone()[0] is not None:
        done = {v for (v,) in conn.execute("SELECT version FROM ext_schema_migrations")}
        if all(f.stem in done for f in files):
            return []
    applied = []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
        conn.execute("CREATE TABLE IF NOT EXISTS ext_schema_migrations ("
                     " version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        done = {v for (v,) in conn.execute("SELECT version FROM ext_schema_migrations")}
        for path in files:
            if path.stem in done:
                continue
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO ext_schema_migrations (version) VALUES (%s)", (path.stem,))
            applied.append(path.stem)
    return applied


def connect(dsn: str) -> psycopg.Connection:
    """An autocommit connection with the extension schema in place."""
    conn = psycopg.connect(dsn, autocommit=True, connect_timeout=5, options="-c statement_timeout=60000")
    try:
        ensure_schema(conn)
    except Exception:
        conn.close()
        raise
    return conn
