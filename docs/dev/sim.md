# Replay of 50 mornings, money and schedule import: developer notes

Three modules on top of the core (`docs/dev/core.md`). They change no core file. None of them touches the
network or calls a model; only `money.case_cost` / `money.today_money` read the database.

```bash
uv run python -m coachcover.sim --mornings 50 --seed 20261109 --out docs/sim   # the replay and its report
uv run python -m coachcover.importers tests/fixtures/import/shifts-generic.csv --tz America/New_York
uv run pytest tests/test_sim.py tests/test_money.py tests/test_importers.py
COACHCOVER_TEST_DSN=postgresql://postgres@127.0.0.1:<port>/postgres uv run pytest   # all, with a test Postgres
```

## `coachcover/sim.py`: 50 training mornings, group chat + phone tree vs CoachCover

- **The data.** The school is `seed.build_school()` turned into rule types (`load_world`). Card trust is the same
  test as the `staff_cert_trust` trigger (migration 003). Each morning (`make_morning(world, index, seed)`) is a
  weekday of the school week, built from `random.Random("coachcover-sim/<seed>/<index>")`. In order, each morning
  draws: 0-3 morning lessons handed to another cleared, free instructor; 2-5 people pushed to 34-40 h this
  week with night blocks from 21:15 to 05:00, when the school has no shifts; 3-7 cards moved to expire around the
  day; maybe one card whose two readings disagree; maybe one program renewal overdue; 1-3 call-outs between 04:30
  and 05:55, at least 15 min before the person's first shift; and, per person, whether they are awake, how fast
  they answer and whether they want the shift. Both strategies run on the same `Morning`, each on its own `State`.
- **Baseline.** The manager sees the call-out after `manager_notice_minutes` and posts it in the staff chat. The
  first "yes" before the start is booked. After `chat_wait_minutes` the manager also phones free people who hold
  the role, in random order. Nothing is checked. With nobody by the start, a lesson goes into the group next to
  it with the norm unchecked; otherwise it is cancelled. A shift without groups stays empty.
- **CoachCover.** It follows the core: `assess` + `rank` over non-reserve people, then offers to eligible people
  in waves (the preset's `offers`: 3 at once, the highest "yes" wins once nobody above it can still answer, at
  most 60 s after the first "yes"), on the policy's timer. The rules run again at "yes". Plan B
  (`plan_b_options`, people already asked excluded) runs when the queue is empty or at the preset's deadline
  (20 min before the start), whichever is first; a "yes" before the manager acts on the plan still covers the
  shift. Order of `Pipeline._plan_facts`: merge, reserve person, overtime, reschedule, close. Each option's mode comes from the policy's autonomy: `propose` waits for the manager, `auto` acts at once.
- **The audit.** Every placement of both strategies goes through `rules.eligible` at the moment of booking. Any
  blocking reason makes it a violation, counted once under the first of role, certificate, program, hours, group
  norm. Approved overtime is excluded, as in the core. Tests prove the audit can see `CERT_EXPIRED`,
  `ROLE_MISSING` and `RATIO_EXCEEDED`, so CoachCover's 0 is a measurement, not an assumption.
- **The output.** `run()` returns everything, including per-morning rows. `write()` produces
  `docs/sim/SIM-50-MORNINGS.json` and `.md`. `stats_block(result)` is the `sim` block of `GET /api/stats`, and
  `sim_stats()` gives the same block from an in-process run cached with `lru_cache`. The Docker image has no
  `docs/`, and one run takes about 0.5 s. A test fails if the committed report differs from a fresh run.
- **What-if rows.** These are not in the main table: `baseline_manager_checks_role` (the manager takes only
  people with the role) and `coachcover_one_offer_at_a_time` (`SimParams(offers="one")`: the core before
  2026-11-07, one offer at a time, plan B only when the queue is empty).
- **Before / after (product decision).** `result["before_after"]` and the report's "Before / after" section put every metric
  of CoachCover one offer at a time next to waves + plan B by the deadline, on the same 50 mornings.

Every parameter is an assumption. All of them are listed with their values in `docs/sim/SIM-50-MORNINGS.md`.

## `coachcover/money.py`: the cost of an uncovered morning and of one cover

- `uncovered_cost(shifts, params)`. A shift with groups adds refunds: participants × lesson price × refund
  share. A shift without groups adds overtime: real hours (in UTC, so a DST night counts right) × wage × 1.5.
  An inspection fine is added once a morning, if one is set.
- `calls_cost(rows)` / `case_cost(conn, case_id)`. Token models are recomputed as tokens × price from
  `model_prices.json`, so a wrong stored cost cannot leak in. Whisper uses the ledger's own `cost_usd`, because the
  ledger keeps no audio seconds. A case pays for its own calls plus 1/N of its call-out's calls (transcript and
  reading the message), where N is the number of cases that call-out opened. The cases of one morning add up to
  the whole ledger.
- `today_money(conn, policy_id, now, manager_minutes=...)` returns `money` with exactly the four keys of the
  contract (`uncovered_morning_usd`, `uncovered_morning_label`, `case_cost_cents`, `manager_hour_usd`), plus
  `card`, the "If nobody had accepted" block. The cost covers today's call-out shifts as if nobody had taken
  them; `case_cost_cents` is for the first case of the day, the one the screen shows.

Prices live in the preset's `money` block (strict loader in `coachcover/presets.py`); each says whether it is an
`assumption` and is returned in `card.assumptions`. Swim school:

| Parameter | Default | Source |
|---|---|---|
| `lesson_price` | $30 per child per group lesson | not an assumption: from the founder's coaching experience (product decision) |
| `refund_share` | 1.0 | assumption: every participant of a lesson that does not run gets a refund or a credit |
| `hourly_wage` | policy `overtime_cost_per_hour` / 1.5 ($22.67 with the preset's $34) | assumption: the preset calls $34 an example; keeping it means the money card and the plan B overtime card use the same rate |
| `overtime_multiplier` | 1.5 | US Fair Labor Standards Act, time and a half over 40 h a week. Every hour of an uncovered shift counts as overtime, which is an upper bound; set 1.0 if the person covering stays under 40 h |
| `inspection_penalty` | $0 | assumption: off unless a school sets a fine or the cost of a closure |
| `manager_hour` | $25 per hour | assumption: conservative placeholder |

A value passed to `money_params(..., lesson_price=28.0)` is marked `source: "set by the school"`.

## `coachcover/importers.py`: schedule import

"We don't replace your schedule — we add the certification layer on top of it."

- `read_csv(source, tz=..., date_order="MDY")` reads a **generic CSV**. Header names are matched loosely
  (`COLUMNS`), including When I Work's words for its objects (Schedule, Job Site, Position, First Name, Last Name,
  Email). A file without start and end columns is read as a staff list. It handles: BOM, `,`/`;`/tab,
  Windows-1252 bytes, US or day-first dates, 12/24-hour times, ISO times with an offset, a time-zone column per
  row, overnight shifts (end ≤ start on one date means the next day), a clock time that does not exist (error)
  or happens twice (warning; the first, daylight time is taken), empty cells (an open shift is a warning),
  duplicates (skipped, with the line of the first), extra cells, and a shift over 24 h (error).
- `read_ics(source, tz=..., window=(first, last))` reads a **generic iCal** (RFC 5545 VEVENT): DTSTART with DTEND
  or DURATION, TZID (IANA, a few Windows names, Mozilla-style paths), UTC, floating times (X-WR-TIMEZONE, else
  `tz`), RRULE DAILY/WEEKLY with INTERVAL/COUNT/UNTIL/BYDAY/WKST at the same wall time across DST, EXDATE,
  RECURRENCE-ID, STATUS:CANCELLED, all-day events (skipped), folded lines and escaped text. Anything else in an
  RRULE is an error. A repeat with no end needs a window. The position is CATEGORIES, else SUMMARY; the place is
  LOCATION; each ATTENDEE (except ROLE=NON-PARTICIPANT) is a person on the shift.
- A bad row becomes a `RowError(line, field, message)` while the other rows import. A whole-file problem raises
  `ImportFormatError`.
- `to_shifts(result, preset, venues=..., staff=..., staff_emails=..., positions=...)` maps rows to rule shifts
  with stable `IMP-<hash>` ids. `import_context` + `check_import` run `rules.eligible` on every assigned shift. In
  the fixture, Ivan's expired card, Javier's expired CPR and Camila's 43.5 h are flagged.

**What the vendors document (checked 2026-11-06), and why the formats are called generic:**
- When I Work, [Exporting Schedules](https://help.wheniwork.com/articles/exporting-schedules/): the export is a
  spreadsheet with two tabs (a list view, then hours and wages). The columns are not listed. To use it, save the
  list tab as CSV.
- When I Work, [Importing Users](https://help.wheniwork.com/articles/importing-users/): CSV or Excel with
  headers; each user needs a first name, a last name, and an email, phone number or employee ID.
- When I Work, [calendar sync](https://help.wheniwork.com/articles/syncing-your-schedule-to-a-calendar-app-computer/):
  uses iCalendar. Admins and managers can sync the entire schedule or one location. The fields inside its events
  are not documented.
- Jackrabbit: according to search results from its help center (the page itself was not opened), a staff
  member's class schedule exports to Excel or PDF. No iCal feed was found.
- iClassPro: no native iCal export was found. A third-party tool (Activity Messenger) publishes iCal feeds of
  iClassPro classes.

So the code makes no claim to read a vendor's format. A real export from each vendor should be run through
`python -m coachcover.importers` before the vendor is named in the submission.

## Fixtures

All synthetic, with invented names and `example.com` addresses. They are in `tests/fixtures/import/`:
`shifts-generic.csv`, `shifts-wiw-words.csv`, `shifts-broken.csv` (one row per error class), `staff.csv`,
`school-monday.csv` (the training school, for the certification check), `schedule-generic.ics` and
`schedule-broken.ics`.
