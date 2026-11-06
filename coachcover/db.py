"""Postgres: schema migrations and preset installation.

Functions take a psycopg connection in autocommit mode and open their own
transaction blocks.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from coachcover.model import Policy
from coachcover.presets import preset_from_dict, read_preset_file

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_LOCK_KEY = 0x636F7665  # advisory lock id for migrations


def migrate(conn: psycopg.Connection) -> list[str]:
    """Apply pending migrations in file-name order. Returns the versions applied now."""
    applied = []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.stem in done:
                continue
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.stem,))
            applied.append(path.stem)
    return applied


def install_preset(conn: psycopg.Connection, code: str) -> None:
    """Copy a preset file into the reference tables. Installing the same file again is a no-op."""
    data = read_preset_file(code)
    preset_from_dict(data)  # validate before writing anything
    with conn.transaction():
        row = conn.execute("SELECT source FROM preset WHERE code = %s", (code,)).fetchone()
        if row is not None:
            if row[0] != data:
                raise ValueError(f"preset {code} is installed with different content")
            return
        conn.execute("INSERT INTO preset (code, label, source) VALUES (%s, %s, %s)", (code, data["label"], Jsonb(data)))
        cert_ids = {}
        for c in data["cert_types"]:
            cert_ids[c["code"]] = conn.execute(
                "INSERT INTO cert_type (preset_code, code, label, validity_months, match)"
                " VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (code, c["code"], c["label"], c["validity_months"], [m.upper() for m in c.get("match", [])]),
            ).fetchone()[0]
        for r in data["roles"]:
            role_id = conn.execute(
                "INSERT INTO role (preset_code, code, label, reserve) VALUES (%s, %s, %s, %s) RETURNING id",
                (code, r["code"], r["label"], r.get("reserve", False)),
            ).fetchone()[0]
            for ord_, cert in enumerate(r["required_certs"]):
                conn.execute(
                    "INSERT INTO role_cert (role_id, cert_type_id, preset_code, ord) VALUES (%s, %s, %s, %s)",
                    (role_id, cert_ids[cert], code, ord_),
                )
        for p in data["programs"]:
            program_id = conn.execute(
                "INSERT INTO program (preset_code, code, label, renewal_months) VALUES (%s, %s, %s, %s) RETURNING id",
                (code, p["code"], p["label"], p["renewal_months"]),
            ).fetchone()[0]
            for ord_, lv in enumerate(p["levels"]):
                conn.execute(
                    "INSERT INTO program_level (program_id, code, label, ord, max_per_instructor)"
                    " VALUES (%s, %s, %s, %s, %s)",
                    (program_id, lv["code"], lv["label"], ord_, lv["max_per_instructor"]),
                )


def insert_policy(conn: psycopg.Connection, policy_id: str, name: str, policy: Policy) -> None:
    conn.execute(
        "INSERT INTO policy (id, name, preset_code, max_weekly_hours, week_start, makeup_window_days, autonomy,"
        " offer_timeout_minutes, overtime_cost_per_hour, default_remedy)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            policy_id,
            name,
            policy.preset.code,
            policy.max_weekly_hours,
            policy.week_start,
            policy.makeup_window_days,
            Jsonb(dict(policy.autonomy)),
            policy.offer_timeout_minutes,
            policy.overtime_cost_per_hour,
            policy.default_remedy,
        ),
    )
