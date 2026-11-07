"""The worker: offer timers and the night check. All its work is state in the database, so a restart loses nothing.

Every tick: expire offers whose timer ran out (the next person is asked), and
after the night-check hour (venue time) write tomorrow's report once. One log
line per tick; after too many failures in a row the process exits so the
container restarts it.

Run: python -m coachcover.worker   (DATABASE_URL in the environment)
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg

from coachcover import ext_db, notify
from coachcover.checks import store_night_report
from coachcover.demo import clock_now
from coachcover.models import DbLedger, ModelClient
from coachcover.pipeline import Pipeline
from coachcover.seed import POLICY_ID


def tick(conn: psycopg.Connection, policy_id: str, now: datetime, night_hour: int = 21) -> dict:
    pipeline = Pipeline(conn, ModelClient(DbLedger(conn)), policy_id)
    expired = pipeline.expire_due(now)
    tz = ZoneInfo(conn.execute("SELECT tz FROM venue WHERE policy_id = %s ORDER BY id LIMIT 1", (policy_id,)).fetchone()[0])
    local = now.astimezone(tz)
    night = None
    if local.hour >= night_hour:
        report, created = store_night_report(conn, policy_id, local.date() + timedelta(days=1), now)
        opened = pipeline.night_cover(report, now) if created else []
        night = {"for_date": report["for_date"], "created": created, "problems": len(report["problems"]),
                 "cases": [c["code"] for c in opened]}
    ext_db.ensure_schema(conn)
    posts = notify.sweep(conn, policy_id, now)  # one team-chat post per closed cover (simulated delivery)
    return {"expired_offers": expired, "night_report": night, "team_posts": len(posts)}


def _log(**fields) -> None:
    print(json.dumps({"at": datetime.now().astimezone().isoformat(timespec="seconds"), **fields}, default=str), flush=True)


def main() -> int:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    policy_id = os.environ.get("COACHCOVER_POLICY_ID", POLICY_ID)
    interval = float(os.environ.get("COACHCOVER_WORKER_INTERVAL", "5"))
    night_hour = int(os.environ.get("COACHCOVER_NIGHT_CHECK_HOUR", "21"))
    max_failures = int(os.environ.get("COACHCOVER_WORKER_MAX_FAILURES", "12"))
    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(now=True))
    failures = 0
    while not stop["now"]:
        try:
            with psycopg.connect(dsn, autocommit=True, connect_timeout=5, options="-c statement_timeout=30000") as conn:
                now = clock_now(conn)
                result = tick(conn, policy_id, now, night_hour)
            failures = 0
            _log(event="tick", clock=now, **result)
        except Exception as exc:  # noqa: BLE001 - logged and counted; the container restarts us after too many
            failures += 1
            _log(event="tick_failed", error=f"{type(exc).__name__}: {exc}", failures=failures)
            if failures >= max_failures:
                _log(event="exit", reason="too many failures in a row")
                return 1
        time.sleep(interval)
    _log(event="stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
