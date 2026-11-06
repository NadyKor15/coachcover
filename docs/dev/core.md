# CoachCover core: developer notes

## Run

```bash
uv sync                      # Python 3.12 venv in .venv, versions pinned in uv.lock
scripts/test-db-up.sh        # Postgres 16 container coachcover-pg-test on 127.0.0.1:55432 (no password, local only)
uv run pytest                # everything; DB tests fail loudly if the container is down
uv run pytest -m "not db"    # pure tests only
scripts/test-db-down.sh      # stop and remove the container
uv run python -m coachcover.seed --dsn postgresql://postgres@127.0.0.1:55432/<db>   # schema + training school
```

Whole stack (api + worker + db; caddy with `--profile edge` on the server):

```bash
POSTGRES_PASSWORD=<random> docker compose up -d --build
curl http://127.0.0.1:8088/health
curl -X POST http://127.0.0.1:8088/api/judge/run
docker compose down -v
```

Live models: `COACHCOVER_MODEL_MODE=live` and `VULTR_INFERENCE_API_KEY` in the shell environment (for example
`infisical run -- docker compose up -d`). The key is never written to a file. Default is `replay`.

## Layout

| File | What |
|---|---|
| `coachcover/model.py` | plain data types; validation (aware datetimes, end after start in UTC, positive ints) |
| `coachcover/presets/*.json`, `coachcover/presets.py` | preset data (roles incl. reserve, card types with title keywords, programs and levels, norms, `venue_term`, policy defaults incl. autonomy, offer timer, overtime price, remedy) and a strict loader |
| `coachcover/rules.py` | `eligible`, `ratio_reason`, `assess`, `rank`: pure functions |
| `coachcover/reasons_text.py` | EN/RU text for reasons (`describe`) and queue places (`describe_place`) |
| `coachcover/migrations/*.sql`, `coachcover/db.py` | schema 1-4, migration runner, preset install |
| `coachcover/repo.py` | rows to the plain data types (read-only) |
| `coachcover/models.py`, `model_prices.json` | model client: allowlist, answer-model check, ledger with cost, replay and live |
| `coachcover/messages.py` | message texts and model prompts/schemas |
| `coachcover/pipeline.py` | call-out, candidates, offers, plan B, closures, card reading |
| `coachcover/checks.py` | night check: tomorrow covered, 30/60-day expiry radar |
| `coachcover/views.py` | read models for the case page, today's screen, stats, ledger |
| `coachcover/api.py`, `coachcover/worker.py`, `coachcover/demo.py` | REST API, worker, demo clock + reset + judge scenario |
| `coachcover/seed.py` | the fictional training school |
| `tests/fixtures/` | synthetic voice notes and cards from the 2026-11-06 model check, recorded and synthetic model answers |
| `scripts/make_recordings.py` | rebuilds the recordings from the check's raw results |

Industry words (swim, pool, lifeguard, WSI...) are allowed only in presets and the seed; a test scans every logic file.

## Rule semantics

- All interval math runs in UTC. Python compares datetimes that share a tzinfo by wall clock, which is wrong across DST.
- A card is valid from 00:00 of its issue date to the end of its expiry date, venue time. Cards of one type that follow each other without a gap cover a shift together; a card that needs review counts only when nothing trusted covers. No printed expiry: issue date plus the type's validity months.
- `CERT_EXPIRES_ON_SHIFT_DAY` is a warning; `today: true` when the expiry day is the local date of `at`.
- Program renewal, like a card: valid to the end of its due day and must cover the whole shift.
- Weekly hours: the policy week in venue time; overlapping bookings count once; a shift crossing the week boundary counts in each week; the limit is inclusive; `hours_after` is rounded up, so an over-limit total never prints as the limit.
- Overlaps are half-open. Not counted against the candidate: the shift itself, the shifts it merges (`merged_shift_ids`), its groups (by kind).
- `Shift.assignee` is the person on the shift or, once open, the person who called out: `IS_THE_ABSENT_PERSON` blocks them.
- Groups on one shift are taught together; their norm is the strictest level's `max_per_instructor` times `shift.instructors`.
- Queue order (product decision): eligible; fewer hours this week; same venue, then distance; program and level match; longer tenure; staff id. `Ranked.place()` is the machine reason.

## Pipeline

- Call-out: voice → `whisper-large-v3-turbo` → text → `muse-glimmer-30b` (`extract_callout` schema) → shifts of today at the named start times (none understood: all of today's remaining shifts) → one case per shift.
- Offers go one at a time to eligible people only (`decision.ok`), reserve roles never; the timer is `offer_timeout_minutes`. At "yes" the rules run again; a blocked "yes" is withdrawn and the next person is asked.
- Autonomy per action: `auto` acts, `propose` leaves a card (`approval` for assignments, `plan_b` row for plan B), `off` does nothing. An option's mode is the strictest of `plan_b` and its own action (`merge_groups`, `overtime`); overtime is never automatic.
- Plan B options, computed by code in this order: merge into a concurrent group within the norm, a reserve person steps in, overtime (price = hours over × `overtime_cost_per_hour`), move (makeup), close (credit or makeup). The text model only writes the lines. Approval needs the shown snapshot hash; if the facts changed since, the plan is superseded and the call fails with 409 and the new plan.
- Family messages: one model-written template per language with `{group}`/`{time}`, filled per group; template text if no model answer. Channel is always `simulated`.
- Cards: two readers (muse, omni). Disagreement, a failed reader, an unknown type or another person's name → flags → `needs_review`. The database recomputes trust from the readings and the stored dates.
- Model replay: recordings are found by (purpose, model, sha256 of the input key). Real recordings come from the 2026-11-06 check; synthetic ones say `"synthetic": true` and their source. The transcript recordings add the `model` field that the check did not keep: verify it live.

## API

`POST /api/callouts` (multipart: `staff_id` + `audio` or `text`) · `GET /api/cases/{id}` · `GET /api/today` · `POST /api/offers/{id}/accept|decline` · `POST /api/plans/{id}/approve` (`hash`, optional `option`; 409 on a changed plan) · `POST /api/approvals/{id}/approve|reject` · `POST /api/closures` · `POST /api/staff/{id}/certificates` (photo) · `GET /api/stats` · `GET /api/models/ledger` · `POST /api/judge/run` (resets the school, plays the morning; `COACHCOVER_JUDGE_RUNS_PER_HOUR` per client address) · `GET /health`.

The case page and today's screen are flat fields plus arrays (`timeline[].line`, `filter_chips[]`, `filter_grid[]`, `offers[]`, `plans[].options[]`, `model_calls[]`, `expiring_30[]`...) for `data-slot` filling.

## Worker

`python -m coachcover.worker`: every tick expires offers whose timer ran out (the next person is asked) and, after `COACHCOVER_NIGHT_CHECK_HOUR` (21) venue time, writes tomorrow's report once. All its work is database state, so a restart loses nothing. One log line per tick; exits after too many failures in a row.

## Training school (`coachcover/seed.py`)

"Today" is Mon 2026-11-09 05:12 America/New_York. Demo call-out matches the recorded Russian voice note: S05 Marina Petrova, `L-1109-G001` (06:00, level 3, next to `L-1109-G002`: 4 + 4 can merge) and `L-1109-G004` (07:00, beginners, next to `L-1109-G003`: 3 + 2 cannot merge at 1:4). Head coaches S06, S12, S23 are in reserve. Judge run: 06:00 is covered by the second person asked (Maria, 05:18); nobody takes 07:00, plan B sends the reserve head coach.
