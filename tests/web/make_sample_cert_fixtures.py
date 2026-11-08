"""Capture the sample certificate check's answers for the web fixtures (replay mode, training school).

Usage: uv run python tests/web/make_sample_cert_fixtures.py [out_dir]   (default web/fixtures/sample-cert)
Needs the test Postgres (scripts/test-db-up.sh); makes its own database and drops it at the end.
Each file is {"status", "retry_after", "body"}: what POST /api/judge/sample-certificate answered.
  agree.json     c3-photo: both readers read the same dates
  disagree.json  h22-photo: the readers read different valid-through dates
  failed.json    c3-photo with Nemotron Omni's recorded answer missing: that reader fails
  limit.json     the 7th judge run of the hour: 429
  busy.json      while a judge run rebuilds the school: 503
  info.json      GET before the first check: mode, hourly limit, runs left
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from coachcover.api import create_app  # noqa: E402
from coachcover.demo import RESET_LOCK, reset_school  # noqa: E402
from tests.conftest import TEST_DSN  # noqa: E402

URL = "/api/judge/sample-certificate"


def keep(res) -> dict:
    return {"status": res.status_code, "retry_after": res.headers.get("retry-after"), "body": res.json()}


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    name = f"cov_fx_{uuid.uuid4().hex[:12]}"
    admin = psycopg.connect(TEST_DSN, autocommit=True)
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    os.environ["COACHCOVER_JUDGE_RUNS_PER_HOUR"] = "6"
    os.environ.pop("COACHCOVER_MODEL_MODE", None)
    os.environ.pop("COACHCOVER_RECORDINGS_DIR", None)
    dsn = make_conninfo(TEST_DSN, dbname=name)
    files: dict[str, dict] = {}
    try:
        with psycopg.connect(dsn, autocommit=True) as conn, TestClient(create_app(dsn)) as client:
            reset_school(conn)
            files["info"] = keep(client.get(URL))
            files["agree"] = keep(client.post(URL, json={"sample": "c3-photo"}))
            files["disagree"] = keep(client.post(URL, json={"sample": "h22-photo"}))
            with tempfile.TemporaryDirectory() as tmp:
                for path in (ROOT / "tests" / "fixtures" / "models").glob("*.json"):
                    if path.name != "card-c3-photo-omni.json":
                        shutil.copy(path, Path(tmp) / path.name)
                os.environ["COACHCOVER_RECORDINGS_DIR"] = tmp
                files["failed"] = keep(client.post(URL, json={"sample": "c3-photo"}))
                del os.environ["COACHCOVER_RECORDINGS_DIR"]
            while True:
                res = client.post(URL)
                if res.status_code == 429:
                    files["limit"] = keep(res)
                    break
                assert res.status_code == 200, res.text
            with psycopg.connect(dsn, autocommit=True) as holder:
                holder.execute("SELECT pg_advisory_lock(%s)", (RESET_LOCK,))
                files["busy"] = keep(client.post(URL))
                holder.execute("SELECT pg_advisory_unlock(%s)", (RESET_LOCK,))
    finally:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()
    for key, value in files.items():
        (out / f"{key}.json").write_text(json.dumps(value, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(key, value["status"], value["body"].get("verdict") or value["body"].get("error") or value["body"].get("mode"))


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "web" / "fixtures" / "sample-cert")
