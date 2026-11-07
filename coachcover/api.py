"""HTTP API for CoachCover, shaped by the API contract (docs/API-CONTRACT-2026-11-06.md).

Settings come from the environment:
  DATABASE_URL                       Postgres DSN (required)
  COACHCOVER_POLICY_ID               default demo-school
  COACHCOVER_MODEL_MODE              replay (default) or live; live also needs VULTR_INFERENCE_API_KEY
  COACHCOVER_SEED_ON_START           1: load the training school when the database is empty
  COACHCOVER_JUDGE_RUNS_PER_HOUR     judge runs allowed per client address per hour (default 6)
  COACHCOVER_WEB_DIR                 the static screens (default: web/ next to the package, when present)
Run: uvicorn --factory coachcover.api:create_app
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

import psycopg
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from coachcover import screens, views
from coachcover.db import migrate
from coachcover.demo import SCENARIOS, clock_now, reset_school, run_scenario
from coachcover.models import DbLedger, ModelClient, ModelError
from coachcover.pipeline import Conflict, NotFound, Pipeline
from coachcover.schedule_import import MAX_IMPORT_BYTES, import_schedule
from coachcover.seed import POLICY_ID

MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
IMAGE_TYPES = {"image/jpeg", "image/png"}
DEFAULT_WEB_DIR = Path(__file__).resolve().parents[1] / "web"


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


class EnrollBody(BaseModel):
    add: int = Field(ge=1, le=100)
    language: str | None = Field(default=None, pattern=r"^[a-z]{2}$")
    by: str = Field(default="front desk", min_length=1, max_length=80)


class JudgeBody(BaseModel):
    scenario: str = "dangerous_morning"


class VolunteerBody(BaseModel):
    staff_id: str
    text: str = Field(min_length=1, max_length=2000)


def _number(ref: str, prefix: str) -> int:
    text = str(ref).removeprefix(prefix)
    if not text.isdigit():
        raise NotFound(f"unknown id {ref}")
    return int(text)


def create_app(dsn: str | None = None, web_dir: Path | None = None) -> FastAPI:
    dsn = dsn or os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set")
    policy_id = os.environ.get("COACHCOVER_POLICY_ID", POLICY_ID)
    runs_per_hour = int(os.environ.get("COACHCOVER_JUDGE_RUNS_PER_HOUR", "6"))
    mode = os.environ.get("COACHCOVER_MODEL_MODE", "replay")
    web = Path(web_dir or os.environ.get("COACHCOVER_WEB_DIR") or DEFAULT_WEB_DIR)

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

    app = FastAPI(title="CoachCover API", version="0.2.0", lifespan=lifespan)

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
        return JSONResponse(status_code=409, content={"reason": str(exc), "error": str(exc), **exc.detail})

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
            return JSONResponse(status_code=503, content={"ok": False, "db": "unreachable", "mode": mode})
        return {"ok": True, "db": "ok", "mode": mode, "model_mode": mode}

    @app.get("/api/today")
    def get_today(venue: str | None = None, conn: psycopg.Connection = Depends(get_conn)):
        return screens.today(conn, policy_id, clock_now(conn), venue, live=mode == "live")

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
        result = pipeline(conn).callout(staff_id, now=now, text=text, audio=data,
                                        filename=(audio.filename if audio else None) or "voice.ogg")
        result["case_id"] = result["cases"][0]["code"] if result["cases"] else None
        return result

    @app.get("/api/cases/{case_ref}")
    def get_case(case_ref: str, download: bool = False, conn: psycopg.Connection = Depends(get_conn)):
        case_id = pipeline(conn).find_case(case_ref)
        page = screens.case_page(conn, policy_id, case_id, live=mode == "live")
        if not download:
            return page
        return JSONResponse(page, headers={"Content-Disposition": f'attachment; filename="case-{page["case_id"]}.json"'})

    @app.post("/api/cases/{case_ref}/volunteer")
    def volunteer(case_ref: str, body: VolunteerBody, conn: psycopg.Connection = Depends(get_conn)):
        p = pipeline(conn)
        return p.volunteer(p.find_case(case_ref), body.staff_id, body.text, clock_now(conn))

    @app.get("/api/offers/{offer_ref}")
    def get_offer(offer_ref: str, conn: psycopg.Connection = Depends(get_conn)):
        return screens.offer_phone(conn, policy_id, offer_ref, clock_now(conn))

    @app.post("/api/offers/{offer_ref}/{answer}")
    def answer_offer(offer_ref: str, answer: str, conn: psycopg.Connection = Depends(get_conn)):
        if answer not in ("accept", "decline"):
            raise HTTPException(404, "use /accept or /decline")
        state = pipeline(conn).respond(_number(offer_ref, "O-"), answer == "accept", clock_now(conn))
        if answer == "accept" and state["status"] != "accepted":
            return JSONResponse(status_code=409, content={
                "reason": state.get("reason") or f"offer is {state['status']}", "state": state["status"]})
        return {"state": state["status"], "case_status": state["case_status"]}

    @app.post("/api/plans/{plan_ref}/approve")
    def approve_plan(plan_ref: str, body: ApproveBody, conn: psycopg.Connection = Depends(get_conn)):
        result = pipeline(conn).approve_plan_b(_number(plan_ref, "P-"), body.hash, clock_now(conn), by=body.by,
                                               option=body.option)
        return {"state": "approved", **result, "plan_id": f"P-{result['plan_id']}"}

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
    async def post_certificate(staff_id: str, photo: UploadFile = File(...), cert_type: str | None = Form(default=None),
                               conn: psycopg.Connection = Depends(get_conn)):
        if photo.content_type not in IMAGE_TYPES:
            raise HTTPException(415, "send a JPEG or PNG photo")
        data = await photo.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(413, "photo is larger than 8 MB")
        return pipeline(conn).read_certificate(staff_id, data, photo.content_type, clock_now(conn), cert_type=cert_type)

    @app.post("/api/groups/{group_id}/enroll")
    def enroll(group_id: str, body: EnrollBody, conn: psycopg.Connection = Depends(get_conn)):
        return pipeline(conn).enroll(group_id, body.add, clock_now(conn), language=body.language, by=body.by)

    @app.post("/api/import", status_code=201)
    async def post_import(file: UploadFile = File(...), tz: str | None = Form(default=None),
                          date_order: str = Form(default="MDY"), venue_id: str | None = Form(default=None),
                          date_from: date | None = Form(default=None), date_to: date | None = Form(default=None),
                          conn: psycopg.Connection = Depends(get_conn)):
        data = await file.read(MAX_IMPORT_BYTES + 1)
        if len(data) > MAX_IMPORT_BYTES:
            raise HTTPException(413, "schedule file is larger than 2 MB")
        if (date_from is None) != (date_to is None):
            raise ValueError("give date_from and date_to together")
        window = (date_from, date_to) if date_from else None
        return import_schedule(conn, policy_id, data, file.filename or "", clock_now(conn), tz=tz,
                               date_order=date_order, default_venue=venue_id, window=window)

    @app.get("/api/stats")
    def get_stats(conn: psycopg.Connection = Depends(get_conn)):
        return views.stats(conn, policy_id)

    @app.get("/api/models/ledger")
    def get_ledger(conn: psycopg.Connection = Depends(get_conn)):
        return views.ledger(conn)

    @app.get("/media/callouts/{name}")
    def callout_audio(name: str, conn: psycopg.Connection = Depends(get_conn)):
        code = name.removesuffix(".ogg")
        row = conn.execute(
            "SELECT a.data, a.mime FROM callout_audio a JOIN cover_case c ON c.callout_id = a.callout_id WHERE c.code = %s",
            (code,),
        ).fetchone()
        if row is None:
            raise NotFound(f"no voice note for {name}")
        return Response(content=bytes(row[0]), media_type=row[1])

    @app.post("/api/judge/run")
    def judge_run(request: Request, body: JudgeBody | None = None, conn: psycopg.Connection = Depends(get_conn)):
        scenario = (body or JudgeBody()).scenario
        if scenario not in SCENARIOS:
            raise ValueError(f"scenario must be one of {SCENARIOS}")
        client = request.client.host if request.client else "unknown"
        migrate(conn)
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
        result = run_scenario(conn, ModelClient(DbLedger(conn)), scenario)
        return {**result, "runs_left_this_hour": runs_per_hour - used - 1}

    if web.is_dir():
        for route, page in (("/offer", "offer.html"), ("/case", "case.html")):
            if (web / page).is_file():
                app.add_api_route(route, (lambda p=web / page: FileResponse(p)), methods=["GET"], include_in_schema=False)
        app.mount("/", StaticFiles(directory=web, html=True), name="web")

    return app
