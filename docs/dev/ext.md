# CoachCover extensions: developer notes

Ideas 5-10 from `docs/IDEAS-BEYOND-SCHEDULE-2026-11-06.md`, as separate modules on top of the core. No core
file is changed. Everything is served by one FastAPI router; the core app adds it with one line:

```python
from coachcover import ext_api
app.include_router(ext_api.router)   # also starts the MCP session manager through the router's lifespan
```

```bash
COACHCOVER_TEST_DSN=postgresql://postgres@127.0.0.1:<port>/postgres uv run pytest tests/test_ext_*.py
```

| File | What |
|---|---|
| `coachcover/ext_api.py` | the router: A2A, network loans, week, what-if, binder, team posts, `/mcp` |
| `coachcover/a2a.py` | staff loans between venues of one network, agent to agent (A2A 1.0, JSON-RPC) |
| `coachcover/mcp_server.py` | MCP server for a manager's assistant (official MCP Python SDK, streamable HTTP) |
| `coachcover/forecast.py` | fragile shifts of the week and their fixes |
| `coachcover/whatif.py` | "what if X is off too?": the pipeline in a rolled-back transaction |
| `coachcover/binder.py` | the inspection binder (PDF, fpdf2) |
| `coachcover/notify.py` | team chat posts after closed covers (simulated delivery) |
| `coachcover/ext_db.py`, `coachcover/ext_migrations/100_ext_network.sql` | the lane's own schema track |
| `coachcover/presets/network/demo_network.json` | the training network: Northside venue, its rules, people, schedule |

Settings: `DATABASE_URL` (or `app.state.dsn`), `COACHCOVER_POLICY_ID`, `COACHCOVER_A2A_PEERS` (JSON
`{venue_id: agent_url}`; default: this server's `/a2a/{venue}`), `COACHCOVER_PUBLIC_URL` (prefix for case
links in team posts), `COACHCOVER_MCP_KEY`, `COACHCOVER_MCP_CALLS_PER_HOUR` (default 120). "Now" is the demo
clock (`coachcover.demo.clock_now`).

## Schema

The lane's tables live in `coachcover/ext_migrations/` and are applied by `ext_db.ensure_schema`, which every
entry point calls (one query when nothing is pending). They are kept out of `coachcover/migrations/` because
`tests/test_db_seed.py::test_migrations_apply_once` lists the core migrations exactly. A demo reset drops the
public schema; the tables and the training network come back on the next call. Tables: `ext_network`,
`ext_network_member`, `ext_loan`, `ext_loan_step` (append-only), `ext_a2a_task`, `ext_cover_request`,
`ext_team_post` (delivery is always `simulated`), `ops.ext_mcp_call` (survives resets).

If the core prefers one migration track: move the file into `coachcover/migrations/`, add `"100_ext_network"` to
that test's list, and keep `ensure_schema` as a no-op check.

## (9) A2A: borrow a person from another venue of the network

Protocol: **A2A 1.0.0** (latest released version on https://a2a-protocol.org/latest/specification/, checked
2026-11-07; earlier: 0.3.0, 0.2.6, 0.1.0). In 1.0 the JSON-RPC method is `SendMessage` (0.3 called it
`message/send`), enums are SCREAMING_SNAKE (`ROLE_USER`, `TASK_STATE_INPUT_REQUIRED`), parts have no `kind`
field (`{"text": ...}`, `{"data": {...}, "mediaType": "application/json"}`), clients send `A2A-Version: 1.0`
(no header means 0.3 and gets `-32009 VersionNotSupportedError`).

- `GET /.well-known/agent-card.json`: one card for the network: `name`, `description`, `supportedInterfaces`
  (one JSONRPC interface per venue, `protocolVersion: "1.0"`), `provider`, `version`, `capabilities`,
  `defaultInputModes`, `defaultOutputModes`, `skills` (one per venue: `staff-loan.<venue>`).
- `POST /a2a/{venue_id}`: JSON-RPC 2.0, methods `SendMessage` and `GetTask`. Data part types:
  `loan.request` (borrower → lender), `loan.recheck` (accepted or not, by the borrower's rules),
  `loan.confirm` (after both approvals). `loan.approve` is refused with `-32004` and reason
  `MANAGER_APPROVAL_REQUIRED`: an agent cannot approve for a manager.
- The lender's task: `TASK_STATE_INPUT_REQUIRED` with the offer, `TASK_STATE_REJECTED` when it declines,
  `TASK_STATE_CANCELED` when the borrower's rules reject the person, `TASK_STATE_COMPLETED` with a
  "Loan record" artifact after confirmation.

Scenario (each step is a row of `ext_loan_step`, written by the side that took it):
1. Borrower agent: an open shift with nobody cleared left (`409` while own cleared people are not asked yet).
2. Lender agent (Northside): its own people by its own rules (Northside: 44 h week), plus the guard check:
   people of the guard role on site or free and cleared during the shift must stay `>= min_guards` after the
   loan (Northside: 1 on site + 3 free = 4 before, 3 after, minimum 3). Otherwise it declines
   (`WOULD_LEAVE_SITE_SHORT`) or has nobody (`NO_CLEARED_PERSON`).
3. Lender offers its best person (Kai Morgan) with cards and bookings.
4. Borrower checks Kai by its own rules (40 h week). Rejected → `loan.recheck accepted=false`, next person.
5. `POST /api/network/loans/{ref}/approve {"manager", "venue"}`: each manager for their own venue (name must be
   that venue's manager, else `403`). One approval changes nothing.
6. Second approval → borrower checks again → `loan.confirm` → lender checks again and records → borrower books
   the shift, closes the case (`shift_covered` event, `via: network_loan`), supersedes plan B, messages the
   person; team posts on both channels.

Demo: the judge scenario `network_loan` plays it on the deck story of the training school (Marina's 06:00 deck
shift; `reset_school(conn, "deck")`). On the preschool morning (08:00, ages 3–5) Northside has nobody with the
preschool training, so a loan for that lesson is declined.

Two agents, two addresses on one machine: run two servers and point the borrower at the lender:
```bash
COACHCOVER_A2A_PEERS='{"V1":"http://127.0.0.1:8091/a2a/V1","N1":"http://127.0.0.1:8092/a2a/N1"}' \
  uvicorn --factory coachcover.api:create_app --port 8091 &   # (with the router included)
COACHCOVER_A2A_PEERS='...same...' uvicorn --factory coachcover.api:create_app --port 8092 &
```
`tests/test_ext_a2a_http.py` does exactly this with two real HTTP servers.

### `GET /api/network/loans/{id or code}`
```json
{"id": 1, "code": "LN-1109-01", "status": "assigned", "status_label": "Loan approved by both managers",
 "network_id": "demo-network",
 "shift": {"id": "L-1109-G002", "label": "Mon 06:00–08:00 · Lifeguard · Riverside Aquatic Center", "time_label": "Mon 06:00–08:00"},
 "case": {"id": 1, "ref": "C-1109-01", "link": "/case.html?id=C-1109-01"},
 "borrower": {"venue_id": "V1", "venue_name": "Riverside Aquatic Center", "manager": "Natalie Brooks",
              "agent_url": "http://…/a2a/V1", "approved": {"by": "Natalie Brooks", "at": "2026-11-09T10:18:00.142Z", "at_label": "10:18:00 UTC"}},
 "lender": {"venue_id": "N1", "venue_name": "Northside Aquatic Center", "manager": "Omar Haddad", "agent_url": "…", "approved": null},
 "candidate": {"staff_id": "N01", "name": "Kai Morgan", "from_venue": "Northside Aquatic Center",
               "line": "Kai first: 8 h this week, Level 3, 8.6 km away, 4.2 yr at this level", "week_hours": 8.0},
 "approvals": 1, "approvals_needed": 2, "created_at": "…Z", "closed_at": null,
 "a2a": {"protocol_version": "1.0", "context_id": "…", "task_id": "…", "task_state": "TASK_STATE_INPUT_REQUIRED"},
 "steps": [{"seq": 1, "at": "2026-11-09T10:18:00.040Z", "at_label": "10:18:00 UTC", "venue_id": "V1", "venue": "Riverside",
            "side": "borrower", "actor": "agent", "kind": "asked_network",
            "title": "Riverside agent: nobody cleared is left here, asks the network",
            "detail": "6 cleared here, 6 asked · Mon 06:00–08:00 · …", "data": {}}],
 "honesty": "Both venues are training venues on one machine; every name is invented."}
```
Statuses: `asking`, `declined` (no venue could lend), `rejected` (offered people failed our rules),
`awaiting_approval`, `assigned`, `failed`. Step kinds: `asked_network`, `request_sent`, `checked_own_people`,
`offered`, `declined`, `checked_offer`, `borrower_accepted`, `offer_withdrawn`, `lender_declined`,
`awaiting_approval`, `approved`, `checked_again`, `confirm_sent`, `lent`, `confirm_refused`, `assigned`,
`no_loan`, `failed`, `lender_unreachable`. Also `GET /api/network` (members, agent URLs) and
`POST /api/network/loans {"shift_id"}` (201, the same shape).

## (8) MCP server

Official MCP Python SDK (`mcp` 1.30, FastMCP), streamable HTTP at `/mcp`, stateless, JSON responses.

| Tool | Does |
|---|---|
| `who_is_allowed(shift_id)` | everyone checked for the shift, in queue order: `allowed[]` with `why_first`, `not_allowed[]` with reasons |
| `shifts_on_day(day, start?)` | shift ids on a day, optionally at a start time (venue time) |
| `fragile_shifts(week_start)` | the week's thin shifts with backups, standby and the best training fix |
| `get_case(case_id)` | a cover case: status, offers, plan B, steps, who covered, link |
| `request_cover(shift_id)` | leaves a proposal for the manager (`ext_cover_request`); sends nothing, books nothing |
| `approve_plan_b(plan_id, hash)` | always refused: "Refused by the autonomy policy (N1): approving plan B is the manager's action…" |

Access: `Authorization: Bearer $COACHCOVER_MCP_KEY` opens the configured school. Without a key: public mode,
only the training school (`demo-school`), `COACHCOVER_MCP_CALLS_PER_HOUR` HTTP requests per client address per
hour (`429` after). A wrong key is `401`.

### README draft: "For AI agents"

> **For AI agents.** CoachCover speaks MCP. Point your assistant at `https://<host>/mcp` (streamable HTTP).
> Ask it "who is allowed on deck tomorrow at 6?" and it calls `shifts_on_day` and `who_is_allowed`; every
> answer carries the reasons from the rules engine (an expired card, the weekly hours, the norm). It can list
> the week's fragile shifts and leave a cover proposal for the manager. It cannot approve anything: approving
> plan B or an assignment stays with the manager, in CoachCover, against the hash of the plan they saw
> (autonomy policy N1). Without a key the endpoint serves the training school with a per-address limit.
>
> Other venues' agents can reach each venue's agent over A2A 1.0: the Agent Card is at
> `/.well-known/agent-card.json`.

## (5) Fragile shifts: `GET /api/week?venue=V1&start=2026-11-08`

Definition (one for the product): backups = people other than the holder whom the core rules clear for the
shift, reserve roles not counted, and only people of the shift's own venue (`forecast.backups_from`, default
`"venue"`; `"policy"` counts every venue of the school). Levels from the threshold `T`
(`forecast.fragile_max_backups`, default 1): `at_risk` 0, `fragile` 1..T, `safe` > T. "N fragile shifts" =
at_risk + fragile; shifts that already started are `past` and not counted. On the training school (main
seed) the week shows 19 fragile shifts across the three venues; with `"policy"` it shows none.

```json
{"venue": {"id": "V1", "name": "…"}, "venues": [{"id": "V1", "name": "…"}],
 "week": {"start": "2026-11-08", "end": "2026-11-14", "label": "Nov 8 – Nov 14"}, "now": "…",
 "threshold": 1, "backups_from": "venue",
 "legend": [{"level": "at_risk", "label": "0 backups", "min": 0, "max": 0, "tone": "red"},
            {"level": "fragile", "label": "1 backup", "min": 1, "max": 1, "tone": "gold"},
            {"level": "safe", "label": "2+ backups", "min": 2, "max": null, "tone": "green"}],
 "past_legend": {"level": "past", "label": "Started or over", "tone": "grey"},
 "counts": {"shifts": 68, "upcoming": 64, "at_risk": 0, "fragile": 3, "safe": 61, "past": 4, "fragile_total": 3},
 "headline": "3 fragile shifts this week",
 "fragile": ["L-1111-G009", "L-1111-G010", "L-1111-G031"],
 "days": [{"date": "2026-11-09", "label": "Mon 9", "cells": [
   {"shift_id": "G-1109-V1-0530", "venue_id": "V1", "venue_name": "…", "day": "2026-11-09",
    "start": "2026-11-09T10:30:00+00:00", "end": "…", "time_label": "05:30–10:00", "role": "lifeguard",
    "role_label": "Lifeguard", "label": "Mon 05:30–10:00 · Lifeguard", "holder": {"staff_id": "S28", "name": "Ava Mitchell"},
    "uncovered": false, "past": false, "backups": 3, "level": "safe",
    "backup_names": ["Olivia Hart", "Rafael Ortiz", "Nikita Morozov"], "other_venues_cleared": 11,
    "standby": null}]}],
 "fixes": {
   "standby": [{"shift_id": "…", "label": "Wed 06:00–06:30 · Swim instructor · Level 6", "level": "fragile",
                "staff_id": "S12", "name": "Noah Bennett", "from_reserve": true, "line": "…",
                "text": "Put Noah Bennett on standby for Wed 06:00–06:30 · Swim instructor · Level 6"}],
   "training": {"best": {"key": "level:learn_to_swim:1", "kind": "level", "label": "Learn-to-Swim Level 1 (beginner)",
                         "people": ["S14", "S18"], "count": 2, "makes_safe": 12, "of": 19, "shift_ids": ["…"],
                         "course_usd_per_person": 300.0, "cost_usd": 600.0,
                         "cost_source": "assumption: placeholder, no course price list in the preset",
                         "text": "Train 2 more in Learn-to-Swim Level 1 (beginner) → 12 of 19 fragile shifts become safe · about $600 (assumed course price)"},
                "options": ["… up to 5, same shape"]}},
 "assumptions": [{"name": "fragile_max_backups", "value": 1, "source": "…"}, "…"]}
```
`standby` is the best cleared person of a reserve role, else the best regular backup (`from_reserve: false`).
Training: every certificate type and every program level of the preset is tried; N people (default 2) are
picked greedily; the test re-runs the rules with the course applied and checks each claimed shift turns safe.

## (6) What if: `POST /api/whatif`

JSON `{"text": "А если завтра и Зои заболеет?"}` or multipart `text` / `audio`. Answer:
```json
{"dry_run": true,
 "question": {"text": "…", "heard_from": "text|voice", "language": "ru"},
 "understood": {"by": "muse-glimmer-30b|fallback", "people": [{"name": "Zoe", "day": "tomorrow", "staff_id": "S17", "staff_name": "Zoe …"}],
                "unknown": [], "ambiguous": []},
 "plan": [{"staff_id": "S17", "staff_name": "…", "date": "2026-11-10", "day": "tomorrow", "reported_at": "…",
           "shifts": [{"shift_id": "L-1110-G005", "label": "…", "time_label": "Tue 06:00–06:30", "cleared": 9,
                       "queue": ["Marina first: 1 h this week, Level 4, same …", "…"],
                       "who_steps_in": {"staff_id": "S05", "name": "Marina Petrova", "line": "…", "needs_manager_approval": false},
                       "if_nobody_says_yes": {"options": [{"kind": "lead", "line": "Noah Bennett (reserve) steps in.", "mode": "propose",
                                                           "cost_usd": null, "families": null, "staff_name": "Noah Bennett"}],
                                              "recommended": "lead",
                                              "norm_breaks": [{"into_shift_id": "L-1110-G006", "participants": 9, "capacity": 8, "norm": "1:8",
                                                               "text": "merging with the Level 2 group at 06:00 would put 9 participants with 1 instructor(s); the norm is 1:8"}]},
                       "cost": {"if_someone_says_yes_usd": 0.0, "if_nobody_says_yes_usd": 0.0, "if_nobody_families_affected": null, "basis": "…"}}]}],
 "summary": {"shifts": 1, "covered": 1, "need_plan_b": 0, "norm_breaks_if_nobody": 1, "worst_case_usd": 0.0,
             "headline": "Zoe … off tomorrow: 1 shift(s) · 1 covered (Marina) · 1 norm break(s) if nobody says yes · worst case $0.00"},
 "model_calls": [{"purpose": "whatif_parse", "model": "muse-glimmer-30b", "source": "recorded", "ok": true, "synthetic": true, "duration_ms": 900, "cost_usd": 6.9e-05}],
 "database": {"before": "<sha256>", "after": "<sha256>", "unchanged": true},
 "note": "Plan only: the pipeline ran in a transaction that was rolled back. Nothing was sent, booked or stored."}
```
Inside the dry run no model is called (the pipeline takes its templates). Model calls of the question itself go
to an in-memory ledger, so the database stays byte-for-byte the same (the tests hash every public table).
Identity counters can skip numbers (Postgres sequences are not transactional). Recordings added:
`tests/fixtures/models/synthetic-ext-whatif-{parse-ru,parse-en,transcribe-ru}.json` (all `"synthetic": true`);
`tests/fixtures/audio/whatif-ru.ogg` is macOS text-to-speech of the sentence.

## (7) Binder: `GET /api/binder.pdf?venue=V1&from=2026-11-09&to=2026-11-09` (and `.json`)

Rows: every scheduled or open shift of the venue in the period, the person, cards valid on that date, the norm
(`8/8 ok (1:4)`), and the rules' result at the shift's start (`ok`, `ok (note: CPR/AED expires today)`,
`VIOLATION: …`). Then replacements (case, absent, outcome, events, sha256 of the canonical JSON of
`[seq, at UTC, actor, kind, data]`), incidents (closures, sha256 of the record), and the total. The digest
(`sha256`, header `X-Binder-SHA256`) covers the records but not the print time. Up to 62 days. Helvetica,
Latin-1: a Cyrillic name would print as `?` (all training names are Latin). A shift the night check caught
and a replacement then covered is not a violation but the line "caught by the nightly check · case C-… ·
covered by …", and the headline adds "· N caught before the shift". On the training week the only holder trap
is S07's Monday 16:00 lesson: 1 violation at V1 without the night check, "0 violations · 1 caught before the
shift" once the night check's case is covered.

## (10) Team chat posts: `GET /api/team-posts`

After each closed cover (`covered` or `plan_b`): `"06:00 covered by Zoe, case C-1109-01"` with
`link: "/case.html?id=C-1109-01"`, channel `#<first word of the venue>-staff`, delivery `simulated`, a log line
`would post to #riverside-staff: …`. Once per case. `GET` runs the sweep; the worker can call
`notify.sweep(conn, policy_id, now)` each tick. A loan posts on both venues' channels.

## How to run the extension tests

```bash
scripts/test-db-up.sh                          # local Postgres for the tests
uv run pytest tests/test_ext_*.py              # the extension lane's tests
uv run pytest tests/test_ext_in_core.py        # every extension route answers through the core app
uv run pytest tests/test_network_loan_scenario.py   # the judge scenario network_loan, agents over HTTP in-process
```

`tests/test_ext_a2a_http.py` starts two real HTTP servers (one per venue) and runs a loan between them. In
`compose.yaml` the two agents are the `api` service (Riverside) and the `agent-north` service (Northside), wired
with `COACHCOVER_A2A_PEERS`.

The core includes the router before its static mount (a mount at "/" answers every route added after it), allows
the preset key `forecast`, shows the covering person from the `shift_covered` event (a lent person is not on the
school's staff list), has the judge scenario `network_loan`, and the worker posts closed covers on every tick.
