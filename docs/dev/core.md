# Coverage core: developer notes

## Run

```bash
uv sync                      # Python 3.12 venv in .venv, versions pinned in uv.lock
scripts/test-db-up.sh        # Postgres 16 container coachcover-pg-test on 127.0.0.1:55432 (no password, local only)
uv run pytest                # everything; DB tests fail loudly if the container is down
uv run pytest -m "not db"    # pure rule tests only
scripts/test-db-down.sh      # stop and remove the container
uv run python -m coverage.seed --dsn postgresql://postgres@127.0.0.1:55432/<db>   # schema + training school
```

## Layout

| File | What |
|---|---|
| `coverage/model.py` | plain data types; validation (aware datetimes, end after start in UTC, positive ints) |
| `coverage/presets/*.json`, `coverage/presets.py` | preset data (roles, card types, programs and levels, norms, policy defaults incl. autonomy) and a strict loader |
| `coverage/rules.py` | `eligible`, `ratio_reason`, `assess`, `rank`: pure functions |
| `coverage/reasons_text.py` | EN/RU text for reasons (`describe`) and queue places (`describe_place`) |
| `coverage/migrations/001_init.sql`, `coverage/db.py` | schema, migration runner, preset install |
| `coverage/repo.py` | rows to the plain data types (read-only) |
| `coverage/seed.py` | the fictional training school |

Industry words (swim, pool, lifeguard...) are allowed only in presets and the seed; a test enforces it.

## Rule semantics

- All interval math runs in UTC. Python compares datetimes that share a tzinfo by wall clock, which is wrong across DST.
- A card is valid from 00:00 of its issue date to the end of its expiry date, in the venue's time zone, and must cover the whole shift. No printed expiry: issue date plus the type's validity months (month end clamped).
- `CERT_EXPIRES_ON_SHIFT_DAY` is a warning: the best card ends on the last day the shift touches. `today: true` when that is also the local date of `at`.
- A card whose two readings disagree (`needs_review`) does not count until a person confirms it (`CERT_NEEDS_REVIEW`, blocking).
- Program renewal is checked against the shift's local start date, inclusive of the due date.
- Weekly hours: the policy week (start day in the policy) in venue time; a shift crossing the week boundary counts in each week by its own hours; the limit is inclusive (exactly 40 of 40 passes).
- Overlaps are half-open (back-to-back passes). The shift itself and its groups are never counted against the candidate (plan B merges).
- Groups on one shift are taught together; their norm is the strictest level's `max_per_instructor` times `shift.instructors`.
- Queue order (product decision): eligible; fewer hours this week; same venue, then distance; program and level match; longer tenure at the level; staff id. `Ranked.place()` is the machine reason, `above_next` names the first factor that decided against the next person.

## Data model notes

- Registry rows use readable text ids (`V1`, `S05`, `G002`, `L-1109-G002`); runtime rows use identity keys.
- `policy.autonomy`: `assign_cover`, `plan_b`, `merge_groups`, `overtime` set to `auto` / `propose` / `off`. Stored and validated; the rules do not read it yet.
- `cover_case` + append-only `case_event` hold the case file of each cover; `offer` and `plan_b` hang off the case.
- DB-enforced: no double booking (`EXCLUDE` on scheduled shifts), one accepted offer per case, one open case per shift, one live plan B per case, messages only `simulated`, a model call that returns another model cannot be `ok`.

## Training school (`coverage/seed.py`)

"Today" is Mon 2026-11-09 05:12 America/New_York. Demo call-out: S05 Olga Petrova, lessons `L-1109-G002` (06:00) and `L-1109-G004` (06:30) at V1. Traps and their ids are listed in the module docstring; `tests/test_db_seed.py` checks each one through the rules. At 05:12, 12 of 39 people are eligible for `L-1109-G002`.
