"""HTTP routes of the extension lane, as one FastAPI router. The core app adds it with one line:

    app.include_router(ext_api.router)

Routes:
  GET  /.well-known/agent-card.json          A2A Agent Card of the network (one card, a skill per venue)
  POST /a2a/{venue_id}                        a venue's agent, A2A 1.0 JSON-RPC (SendMessage, GetTask)
  GET  /api/network                           members of the network and their agents
  POST /api/network/loans                     the borrower's agent asks the network for a person {shift_id}
  GET  /api/network/loans/{ref}               a loan with its steps (UTC stamps) for the screen
  POST /api/network/loans/{ref}/approve       a manager approves for their own venue {manager, venue}
  GET  /api/week?venue=&start=                fragile shifts of a week and what would fix them
  POST /api/whatif                            "what if X is off too?": a plan without actions (dry run)
  GET  /api/binder.pdf?venue=&from=&to=       the inspection binder for a period
  GET  /api/team-posts                        team chat posts (simulated delivery); POST runs the sweep
  /mcp                                        MCP server (streamable HTTP) for a manager's assistant

Settings: DATABASE_URL (or app.state.dsn), COACHCOVER_POLICY_ID, COACHCOVER_A2A_PEERS, COACHCOVER_MCP_KEY,
COACHCOVER_MCP_CALLS_PER_HOUR. The demo clock (coachcover.demo.clock_now) is "now" everywhere.
"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import asynccontextmanager
from datetime import date
from typing import Iterator

import psycopg
from anyio import to_thread
from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from coachcover import a2a, ext_db, forecast, mcp_server, notify
from coachcover.demo import clock_now
from coachcover.seed import POLICY_ID

MAX_AUDIO_BYTES = 10 * 1024 * 1024


def policy_id() -> str:
    return os.environ.get("COACHCOVER_POLICY_ID", POLICY_ID)


def dsn_of(app) -> str:
    dsn = getattr(app.state, "dsn", None) or os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set")
    return dsn


def connect(dsn: str) -> psycopg.Connection:
    return ext_db.connect(dsn)


def get_conn(request: Request) -> Iterator[psycopg.Connection]:
    with connect(dsn_of(request.app)) as conn:
        yield conn


def base_url(request: Request) -> str:
    return str(request.base_url)


@asynccontextmanager
async def _lifespan(app):
    async with mcp_server.running(app):
        yield


router = APIRouter(lifespan=_lifespan)
router.add_route("/mcp", mcp_server.Endpoint(), methods=["GET", "POST", "DELETE"], include_in_schema=False)


def _error(exc: Exception, status: int) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": str(exc), "detail": getattr(exc, "detail", {})})


def _call(fn, *args, **kwargs):
    """Run a lane function; its known errors become JSON answers with their status codes."""
    try:
        return fn(*args, **kwargs)
    except a2a.LoanError as exc:
        return _error(exc, exc.status)
    except ValueError as exc:
        return _error(exc, 422)


# --- A2A ------------------------------------------------------------------------------

@router.get("/.well-known/agent-card.json")
def agent_card(request: Request, conn: psycopg.Connection = Depends(get_conn)):
    a2a.ensure_network(conn)
    card = a2a.agent_card(conn, base_url(request))
    etag = '"' + hashlib.sha256(json.dumps(card, sort_keys=True).encode()).hexdigest()[:32] + '"'
    return JSONResponse(card, headers={"Cache-Control": "max-age=300", "ETag": etag})


@router.post("/a2a/{venue_id}")
async def a2a_rpc(venue_id: str, request: Request):
    raw = await request.body()
    version = request.headers.get("A2A-Version") or request.query_params.get("A2A-Version")
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(a2a.rpc_error(None, a2a.RpcError(a2a.PARSE_ERROR, "Invalid JSON payload", "PARSE_ERROR")))
    def work():
        with connect(dsn_of(request.app)) as conn:
            a2a.ensure_network(conn)
            return a2a.handle_rpc(conn, venue_id, body, version, clock_now(conn))

    return JSONResponse(await to_thread.run_sync(work))


class LoanStart(BaseModel):
    shift_id: str = Field(min_length=1, max_length=80)


class LoanApprove(BaseModel):
    manager: str = Field(min_length=1, max_length=120)
    venue: str = Field(min_length=1, max_length=40)


@router.get("/api/network")
def network(request: Request, conn: psycopg.Connection = Depends(get_conn)):
    a2a.ensure_network(conn)
    return a2a.network_summary(conn, base_url(request))


@router.post("/api/network/loans", status_code=201)
def start_loan(body: LoanStart, request: Request, conn: psycopg.Connection = Depends(get_conn)):
    return _call(a2a.start_loan, conn, policy_id(), body.shift_id, clock_now(conn), base_url(request))


@router.get("/api/network/loans/{ref}")
def get_loan(ref: str, request: Request, conn: psycopg.Connection = Depends(get_conn)):
    a2a.ensure_network(conn)
    return _call(a2a.loan_view, conn, ref, base_url(request))


@router.post("/api/network/loans/{ref}/approve")
def approve_loan(ref: str, body: LoanApprove, request: Request, conn: psycopg.Connection = Depends(get_conn)):
    return _call(a2a.approve, conn, ref, body.manager, body.venue, clock_now(conn), base_url(request))


# --- fragile shifts of the week (idea 5) -------------------------------------------------------

@router.get("/api/week")
def week(venue: str | None = None, start: date | None = None, conn: psycopg.Connection = Depends(get_conn)):
    return _call(forecast.week, conn, policy_id(), clock_now(conn), venue, start)


# --- team chat posts (idea 10) -------------------------------------------------------------

@router.get("/api/team-posts")
def team_posts(venue: str | None = None, conn: psycopg.Connection = Depends(get_conn)):
    notify.sweep(conn, policy_id(), clock_now(conn))
    return {"delivery": "simulated", "posts": notify.posts(conn, venue)}


@router.post("/api/team-posts/sweep")
def team_posts_sweep(conn: psycopg.Connection = Depends(get_conn)):
    return {"delivery": "simulated", "new": notify.sweep(conn, policy_id(), clock_now(conn))}
