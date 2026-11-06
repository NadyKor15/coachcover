"""HTTP API for CoachCover. Thin routes over the pipeline and the read models.

Settings come from the environment:
  DATABASE_URL                       Postgres DSN (required)
  COACHCOVER_POLICY_ID               default demo-school
  COACHCOVER_MODEL_MODE              replay (default) or live; live also needs VULTR_INFERENCE_API_KEY
  COACHCOVER_SEED_ON_START           1: load the training school when the database is empty
  COACHCOVER_JUDGE_RUNS_PER_HOUR     judge runs allowed per client address per hour (default 6)
Run: uvicorn --factory coachcover.api:create_app
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Iterator

import psycopg
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from coachcover import views
from coachcover.db import migrate
from coachcover.demo import clock_now, reset_school, run_judge_scenario
from coachcover.models import DbLedger, ModelClient, ModelError
from coachcover.pipeline import Conflict, NotFound, Pipeline
from coachcover.seed import POLICY_ID

MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
IMAGE_TYPES = {"image/jpeg", "image/png"}


class ApproveBody(BaseModel):
    hash: str = Field(min_length=64, max_length=64)
    option: int | None = Field(default=None, ge=0)
    by: str = Field(default="manager", min_length=1, max_length=80)


class ClosureBody(BaseModel):
    venue_id: str
    zone_id: str | None = None
    reason: str
    starts_at: datetime
    ends_at: datetime
    remedy: str | None = None
    by: str = Field(default="manager", min_length=1, max_length=80)


class DecideBody(BaseModel):
    by: str = Field(default="manager", min_length=1, max_length=80)


def create_app(dsn: str | None = None) -> FastAPI:
    dsn = dsn or os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set")
    policy_id = os.environ.get("COACHCOVER_POLICY_ID", POLICY_ID)
    runs_per_hour = int(os.environ.get("COACHCOVER_JUDGE_RUNS_PER_HOUR", "6"))

    def connect() -> psycopg.Connection:
        return psycopg.connect(dsn, autocommit=True, connect_timeout=5, options="-c statement_timeout=60000")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        with connect() as conn:
            migrate(conn)
            has_school = conn.execute("SELECT count(*) FROM policy WHERE id = %s", (policy_id,)).fetchone()[0]
            if not has_school and os.environ.get("COACHCOVER_SEED_ON_START") == "1":
                reset_school(conn)
        yield

    app = FastAPI(title="CoachCover API", version="0.1.0", lifespan=lifespan)

    def get_conn() -> Iterator[psycopg.Connection]:
        with connect() as conn:
            yield conn

    def pipeline(conn: psycopg.Connection) -> Pipeline:
        return Pipeline(conn, ModelClient(DbLedger(conn)), policy_id)

    @app.exception_handler(NotFound)
    async def _not_found(_: Request, exc: NotFound):
        return JSONResponse(status_code=404, content={"error": str(exc)})

    @app.exception_handler(Conflict)
    async def _conflict(_: Request, exc: Conflict):
        return JSONResponse(status_code=409, content={"error": str(exc), "detail": exc.detail})

    @app.exception_handler(ValueError)
    async def _bad_value(_: Request, exc: ValueError):
        return JSONResponse(status_code=422, content={"error": str(exc)})

    @app.exception_handler(ModelError)
    async def _model_failed(_: Request, exc: ModelError):
        return JSONResponse(status_code=502, content={"error": f"model step failed: {exc}"})

    @app.get("/health")
    def health():
        try:
            with connect() as conn:
                conn.execute("SELECT 1")
        except psycopg.Error:
            return JSONResponse(status_code=503, content={"ok": False, "db": "unreachable"})
        return {"ok": True, "db": "ok", "model_mode": os.environ.get("COACHCOVER_MODEL_MODE", "replay")}

    @app.post("/api/callouts", status_code=201)
    async def post_callout(
        staff_id: str = Form(...),
        text: str | None = Form(default=None),
        audio: UploadFile | None = File(default=None),
        conn: psycopg.Connection = Depends(get_conn),
    ):
        data = None
        if audio is not None:
            data = await audio.read(MAX_AUDIO_BYTES + 1)
            if len(data) > MAX_AUDIO_BYTES:
                raise HTTPException(413, "audio is larger than 10 MB")
            if not data:
                raise ValueError("empty audio file")
        now = clock_now(conn)
        return pipeline(conn).callout(staff_id, now=now, text=text, audio=data,
                                      filename=(audio.filename if audio else None) or "voice.ogg")

    @app.get("/api/cases/{case_id}")
    def get_case(case_id: int, conn: psycopg.Connection = Depends(get_conn)):
        page = views.case_page(conn, policy_id, case_id)
        if not page:
            raise NotFound(f"unknown case {case_id}")
        return page

    @app.get("/api/today")
    def get_today(conn: psycopg.Connection = Depends(get_conn)):
        return views.today_page(conn, policy_id, clock_now(conn))

    @app.post("/api/offers/{offer_id}/{answer}")
    def answer_offer(offer_id: int, answer: str, conn: psycopg.Connection = Depends(get_conn)):
        if answer not in ("accept", "decline"):
            raise HTTPException(404, "use /accept or /decline")
        return pipeline(conn).respond(offer_id, answer == "accept", clock_now(conn))

    @app.post("/api/plans/{plan_id}/approve")
    def approve_plan(plan_id: int, body: ApproveBody, conn: psycopg.Connection = Depends(get_conn)):
        return pipeline(conn).approve_plan_b(plan_id, body.hash, clock_now(conn), by=body.by, option=body.option)

    @app.post("/api/approvals/{approval_id}/{decision}")
    def decide(approval_id: int, decision: str, body: DecideBody, conn: psycopg.Connection = Depends(get_conn)):
        if decision not in ("approve", "reject"):
            raise HTTPException(404, "use /approve or /reject")
        return pipeline(conn).decide_approval(approval_id, decision == "approve", clock_now(conn), by=body.by)

    @app.post("/api/closures", status_code=201)
    def post_closure(body: ClosureBody, conn: psycopg.Connection = Depends(get_conn)):
        if body.starts_at.tzinfo is None or body.ends_at.tzinfo is None:
            raise ValueError("starts_at and ends_at need a time zone offset")
        return pipeline(conn).closure(body.venue_id, body.zone_id, body.reason, body.starts_at, body.ends_at,
                                      now=clock_now(conn), created_by=body.by, remedy=body.remedy)

    @app.post("/api/staff/{staff_id}/certificates", status_code=201)
    async def post_certificate(staff_id: str, photo: UploadFile = File(...), conn: psycopg.Connection = Depends(get_conn)):
        if photo.content_type not in IMAGE_TYPES:
            raise HTTPException(415, "send a JPEG or PNG photo")
        data = await photo.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(413, "photo is larger than 8 MB")
        return pipeline(conn).read_certificate(staff_id, data, photo.content_type, clock_now(conn))

    @app.get("/api/stats")
    def get_stats(conn: psycopg.Connection = Depends(get_conn)):
        return views.stats(conn, policy_id)

    @app.get("/api/models/ledger")
    def get_ledger(conn: psycopg.Connection = Depends(get_conn)):
        return views.ledger(conn)

    @app.post("/api/judge/run")
    def judge_run(request: Request, conn: psycopg.Connection = Depends(get_conn)):
        client = request.client.host if request.client else "unknown"
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"judge:{client}",))
            used = conn.execute(
                "SELECT count(*) FROM ops.judge_run WHERE client = %s AND at > now() - interval '1 hour'", (client,)
            ).fetchone()[0]
            if used >= runs_per_hour:
                retry = conn.execute(
                    "SELECT ceil(extract(epoch FROM min(at) + interval '1 hour' - now())) FROM ops.judge_run"
                    " WHERE client = %s AND at > now() - interval '1 hour'",
                    (client,),
                ).fetchone()[0]
                return JSONResponse(status_code=429, headers={"Retry-After": str(int(retry or 60))},
                                    content={"error": f"limit of {runs_per_hour} judge runs per hour reached"})
            conn.execute("INSERT INTO ops.judge_run (client) VALUES (%s)", (client,))
        reset_school(conn)
        result = run_judge_scenario(conn, ModelClient(DbLedger(conn)))
        return {
            "cases": result["cases"],
            "case_urls": {shift: f"/api/cases/{case}" for shift, case in result["cases"].items()},
            "timeline": result["timeline"],
            "plan_b": result["plan_b"],
            "ended_at": result["ended_at"],
            "runs_left_this_hour": runs_per_hour - used - 1,
        }

    return app
