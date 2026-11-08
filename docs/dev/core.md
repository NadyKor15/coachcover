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
| `coachcover/planb.py` | `plan_b_options`: pure plan B options (the pipeline and the 50-morning replay both call it) |
| `coachcover/reasons_text.py` | EN/RU text for reasons (`describe`) and queue places (`describe_place`) |
| `coachcover/migrations/*.sql`, `coachcover/db.py` | schema 1-7, migration runner, preset install |
| `coachcover/repo.py` | rows to the plain data types (read-only) |
| `coachcover/models.py`, `model_prices.json` | model client: allowlist, answer-model check, ledger with cost, replay and live |
| `coachcover/messages.py` | message texts and model prompts/schemas |
| `coachcover/pipeline.py` | call-out, candidates, offers, plan B, closures, card reading |
| `coachcover/checks.py` | night check: tomorrow covered, 30/60-day expiry radar |
| `coachcover/screens.py` | the contract shapes: `today` (screen A), `offer_phone` (B), `case_page` (C) |
| `coachcover/views.py` | stats (contract keys + `sim`), audit of eligibility violations, model ledger |
| `coachcover/schedule_import.py` | `POST /api/import`: the school's CSV/iCal → `importers` → shifts → the rules over them |
| `coachcover/sim.py`, `money.py`, `importers.py` | SIM lane: 50-morning replay, money card, schedule file readers (`docs/dev/sim.md`) |
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
- A person who called out is offered nothing in their absence window, at least their whole local day (`Context.absences`, loaded by `repo.load_absences` from call-out cases): `CALLED_OUT`, on screen "Called out today" (sick or a burst pipe: the message says why).
- Groups on one shift are taught together; their norm is the strictest level's `max_per_instructor` times `shift.instructors`; a group whose youngest child is at most `young_children.max_age` uses `young_children.max_per_instructor` (1:4) when that is stricter.
- `shift.extra_certs`: cards this one shift needs on top of the role's (the deck story's hero shift also needs the WSI card).
- A program with one level (preset `programs[].levels`) is named by the program alone on screens (`Program.title`); an optional `clearance` text names what a person must hold ("preschool training (ages 3–5)": the chip "No preschool training (ages 3–5)", the case line "Needs: Instructor · Preschool training (ages 3–5)").
- A lesson with two instructors is two shifts teaching the same group at the same time: the lesson and a seat `<shift>-I<n>` with `extra_for` (as the live norm opens them). Screens name the other teacher in the shift line ("· with Emma Carter"); plan B and the replay never treat the seat as a group next door.
- Revoked cards (idea 2): `issuer_registry` (SIMULATED in the demo: training data, `simulated = true`, the screen label says "(simulated registry)") is matched by card type and number without spaces/dashes. A revoked card stops at 00:00 of its revocation day; when the printed dates would have covered the shift, the reason is `CERT_REVOKED` ("Valid until Jun 2028 — REVOKED by issuer on Jul 24"). Seed: Carlos Mendoza (S35, lifeguard card), revoked 2026-07-24; in the preschool story also Irina Lebedeva (S14, WSI), revoked 2026-10-12.
- Rest (idea 3): preset `min_rest_hours` (10). Work on an earlier local day must end, and work on a later day must start, at least that far from the shift: `REST_TOO_SHORT` ("Only 9 h rest since 21:00" / "... before 6:00"). Work on the shift's own start day is one working day (a split shift), not a rest question. Seed: Gabriela Vargas closes Lakeview on Sunday at 21:00, 9 h before the deck story's 06:00 shift, so that morning is 40 → 5 (the preschool morning at 09:30 has nobody short of rest: Gabriela has no preschool training); the greedy fill never breaks the rule.
- Queue order (product decision): eligible; fewer hours this week; same venue, then distance; program and level match; longer tenure; then (idea 4) speaks the language of more than half of the group's families (`lesson_group.family_counts`, `staff.languages`); staff id. `Ranked.place()` is the machine reason; `describe_place` gives the line "Zoe first: 12 h this week, Level 3, same pool".

## Pipeline

- Call-out: voice → `whisper-large-v3-turbo` → text → `muse-glimmer-30b` (`extract_callout` schema) → shifts of today at the named start times (none understood: all of today's remaining shifts) → one case per shift.
- Offers (product decision) go in waves of the preset's `offers.wave_size` (3) to the next eligible people in queue order (`decision.ok`), reserve roles never; the timer is `offer_timeout_minutes`. A "yes" is `held` while someone higher in the wave can still answer, at most `offers.collect_seconds` (60) after the first "yes"; then the highest held "yes" gets the shift (the rules run again) and every other open offer stands down ("thanks — Zoe already covered …"). A "yes" after that is a 409 with the reason "Thanks — Zoe already covered it". A wave nobody answers in time is followed by the next one. The worker's tick (`expire_due`) closes windows and sends waves. The "why" line names the place in the wave ("Zoe third: …").
- Plan B by the deadline: `offers.plan_b_minutes_before_start` (20) before the start, an open case gets plan B even while offers are out (event `plan_b_deadline`); a later "yes" still covers the shift and the proposed plan becomes `superseded`.
- Autonomy per action: `auto` acts, `propose` leaves a card (`approval` for assignments, `plan_b` row for plan B), `off` does nothing. An option's mode is the strictest of `plan_b` and its own action (`merge_groups`, `overtime`); overtime is never automatic.
- Plan B options, computed by code in this order: merge into a concurrent group within the norm, a reserve person steps in, overtime (price = hours over × `overtime_cost_per_hour`), move (makeup), close (credit or makeup). The text model only writes the lines. Approval needs the shown snapshot hash; if the facts changed since, the plan is superseded and the call fails with 409 and the new plan.
- Plan B never suggests a person already asked in the case (declined, no reply, withdrawn), not even as overtime. An approved option that cannot be carried out marks the plan `failed` (event `plan_b_failed`) and a new plan is proposed.
- Message filter: every call-out and volunteer message goes through `nemotron-3.5-content-safety`; it only flags (`flagged[]`, event `message_flagged`), the rules still decide. Anna's "just assign me anyway" is flagged and still blocked by her expired card (preschool story: "My WSI card expired last week…", a prepared filter answer; deck story: the recorded answer to "My lifeguard certificate expired…").
- Night check (`night_cover` action, `propose` by default): a shift of tomorrow whose holder fails the rules gets a cover case with an approval card.
- Live norm (idea 1): `POST /api/groups/{id}/enroll {add, language?, by}` grows the group and re-checks its shifts of the coming week. A shift over the norm gets the missing instructor seats as shifts teaching the same groups at the same time (`extra_for`, id `<shift>-I<n>`, `instructors` raised on all of them) and a cover case each (`source = 'ratio'`, event `ratio_broken`); the same queue fills them. `/api/today.ratio_alert`: red while open, green "Covered by …" once filled. Plan B of a ratio case (`planb.ratio_plan_options`) never leaves a lesson over the norm: (a) a reserve person takes the seat; (b) `split` — another cleared instructor teaching at the same time takes the newest children as a group of their own level (a new `lesson_group` `<group>-S<case>` on that shift); (c) `move` — the newest children (by `group_enrollment`) go to the nearest later lesson of their level with room, families told in their language; with none of these, `cancel_lesson` cancels the lesson and its seats with credits. After (b)/(c) the open seats close and the lesson keeps the instructors it has. A case closed by plan B withdraws its open offers. `tests/test_ratio_plan_b.py` checks the invariant over all 120 groups of the training school and every option offered (236 applied on the Mon 9 - Sun 15 week).
- Family messages: one model-written template per language with `{group}`/`{time}`, filled per group; template text if no model answer. Channel is always `simulated`.
- Cards: two readers (muse, omni). Disagreement on type or dates, a failed reader, an unknown type or another person's name → flags → `needs_review`; readers that agree on everything but the card number → trusted with the note `check_id`. The database recomputes trust from the readings and the stored dates.
- Model replay: recordings are found by (purpose, model, sha256 of the input key). Real recordings come from the 2026-11-06 check; synthetic ones say `"synthetic": true` and their source. The transcript recordings add the `model` field that the check did not keep: verify it live.
- Test certificate photos and their recorded model readings keep the earlier fictional names; the demo data uses the new ones.

## Private lessons (one child, one coach)

- Data: `lesson_group.kind = 'private'` (migration 009), size 1, `participant` = the child's first name (the parent is
  never named), 30 min. Seed (preschool story only; the deck story is unchanged): Marina's Mon 17:00 "Private · Lily, age
  6, Level 2" (`HERO_PRIVATE`, `L-1109-P001`) and Tue 17:00 (Vera), plus 1-2 for every coach whose week is not fixed for
  the demo (`FIXED_ONLY` keeps its hours: the morning's queue lines do not change), placed after every other booking so
  nothing moves. Price: the preset's `money.private_lesson_price` ($50, the founder's coaching practice; `seed.PRIVATE_LESSON_USD`
  matches it).
- A call-out opens every shift of the person's day (they are out for the whole day): the shifts the message names first,
  then the rest (`shift_opened.why = "out for the whole day"`).
- Flow: the same queue and waves. The winning "yes" of a private lesson is not booked: `_ask_family` holds the coach
  (offer `held` + `client_ask` `asked`; `repo.load_context` counts the hold as their time, so no other offer overlaps it),
  stands the rest of the wave down and asks the family (text by the text model, purpose `family_question`, in the
  family's language; template otherwise; replay: `synthetic-family-question-lily-*.json`). `family_respond`: yes → the
  rules again → `_take`; failed → the next wave, the family is asked again; no → the coach is released, `own_coach_slot`
  (the same local start time on the next 7 days, the same rules) → a `reschedule` question; yes → `_move_private` (a new
  shift `L-MMDD-Pnnn`, the old one cancelled); no or no time → `_credit_private` (remedy credit, `amount_usd` 50). The
  answer window: `offers.client_answer_minutes` (30), at the latest `client_answer_before_start_minutes` (60) before the
  start; the worker's tick (`expire_due` → `_client_due`) marks silence, releases the coach and proposes plan B
  (`planb.private_plan_options`: own coach at the nearest free time, credit) for the manager's hash approval.
- A private lesson is never a merge partner or a move target of a ratio plan; the morning money card leaves it out
  (Today shows its own card when the screen's case is a private lesson).
- API: `GET /api/client/{Q-n}` (the family's page), `POST /api/client/{Q-n}/respond {"accept"}` (409 once closed). Judge
  scenarios `private_lesson` and `private_declined` play Marina's own midday note (`tests/fixtures/audio/ru-callout-private-1700.ogg`,
  `say -v Milena -r 150`, the name stressed "Ли́ли" so live whisper hears "Лили": "…Я заболела, в пять не смогу провести
  индивидуальное занятие с Лили…", prepared answers
  `synthetic-*-ru-private-1700.json`) at 12:10:10: her 09:30 lesson is over, so the whole-day rule opens one case, the 17:00
  private lesson (C-1109-01). Chloe says yes 12:12, the family is asked (answer by 12:42); `private_lesson`: yes at 12:16;
  `private_declined`: no at 12:14:30 → Wed 17:00 with Marina, yes at 12:16. The morning scenarios keep the 08:40 note (two
  cases). Web fixtures of these scenes: `cases/C-1109-01.private_*.json`, `offers/<Chloe's>.private_lesson.json`
  (`&mock=private_lesson|private_declined`; Today's links add it after a judge run of that scene). MCP: `get_case` shows
  `family_questions`; `answer_for_family` is always refused.
- Case page Proof: one row per model call (`model_calls[]`: `time_label` "08:40:10 ET", `purpose_label`, `seconds_label`,
  `cost_label` in cents rounded so the rows add up to `model_cost_label`, `shared_label` "shared by 2 cases" for a
  call-out's call, `source_label` live / recorded / prepared) and a total `model_calls_total_label` = the Models line.

## API

Shapes: `docs/API-CONTRACT-2026-11-06.md` and `web/fixtures/*.json`; `tests/test_contract.py` checks every fixture key and type against the live answers.

`GET /api/today?venue=` · `POST /api/callouts` (multipart: `staff_id` + `audio` or `text`) · `GET /api/cases/{C-MMDD-NN}` (`?download=true`) · `POST /api/cases/{id}/volunteer` · `GET /api/offers/{O-n}` · `POST /api/offers/{O-n}/accept|decline` (409 `{"reason"}` when the rules refuse the yes) · `POST /api/plans/{P-n}/approve` (`hash`, optional `option`; 409 `{"reason": "plan changed", "current_hash"}`) · `POST /api/approvals/{id}/approve|reject` · `POST /api/closures` · `GET /api/staff` (read-only roster with each card's state today, from `views.staff_directory`) · `POST /api/staff/{id}/certificates` (photo, optional `cert_type`) · `POST /api/import` · `POST /api/groups/{id}/enroll` · `GET /api/stats` · `GET /api/models/ledger` · `GET /media/callouts/{code}.ogg` · `POST /api/judge/run {"scenario"}` (`dangerous_morning`, `nobody_accepts`, `nightly_check`; resets the school, `COACHCOVER_JUDGE_RUNS_PER_HOUR` per client address, the limit lives in `ops` and survives the reset) · `GET /health` (`mode`), plus the extension router (`coachcover/ext_api.py`, `docs/dev/ext.md`: `/api/week`, `/api/network/…`, `/a2a/{venue}`, `/.well-known/agent-card.json`, `/api/whatif`, `/api/binder.pdf`, `/api/team-posts`, `/mcp`), included before the static mount. The screens in `web/` are served from `/`, plus `/offer` and `/case`. `money.source` on `/api/today` is the lesson price's source; `outcome.covered_time` is the moment the case closed; model costs print through `money.cost_label` (dollars from one cent, else cents).

The case page and today's screen are flat fields plus arrays (`timeline[].line`, `filter_chips[]`, `filter_grid[]`, `offers[]`, `plans[].options[]`, `model_calls[]`, `expiring_30[]`...) for `data-slot` filling.

## Schedule import (`POST /api/import`)

"We don't replace your schedule — we add the certification layer on top of it."

Multipart: `file` (CSV or iCal, ≤ 2 MB), optional `tz` (default: the venues' zone), `date_order` (`MDY`/`DMY`), `venue_id` (for rows without a known place), `date_from` + `date_to` (bounds repeating iCal events). Places are matched by venue name, people by exact name, positions by role label. Bad rows are named (`errors`: line, field, why) and skipped; good rows are saved as shifts with stable ids `IMP-…` (re-importing the same file saves nothing new: `already_there`). A row with nobody, or with a name not on the staff list, is saved `open`, so the night check sees the gap. A row that would book a person twice at the same time is not saved (`not_saved`) and its check names the overlap. Every assigned row is checked at the demo clock against the rules over the imported schedule plus the stored one: `checks[]` with `ok`, `blocking[]`, `warnings[]` (rule codes), `line`, `name`, `saved`. A blocked row is still saved: it is the school's schedule, and the night check keeps flagging it.

## Worker

`python -m coachcover.worker`: every tick expires offers whose timer ran out (the next person is asked) and, after `COACHCOVER_NIGHT_CHECK_HOUR` (21) venue time, writes tomorrow's report once. All its work is database state, so a restart loses nothing. One log line per tick; exits after too many failures in a row.

## Training school (`coachcover/seed.py`)

Two stories of the same Monday (`build_school(story)`, `reset_school(conn, story)`, `seed --story`); every judge scenario plays `preschool`; `deck` is kept for the tests of the older 06:00 shift. The school's week is Mon 9 - Sun 15 (policy week starts Monday; `/api/week` starts today by default); Gabriela's Sunday evening block is the eve, Nov 8. Head coaches in reserve stay free before 08:00 every day. Lesson price $30 per child (from the founder's coaching experience): the uncovered morning is 8 × $30 = $240. Riverside Aquatic Center (V1), Maplewood Swim Center (V2), Lakeview Natatorium (V3); manager Natalie Brooks; 40 people.

**preschool** (default, the founder's morning): demo clock Mon 2026-11-09 08:40 America/New_York; the lesson time and the call-out time are one pair in `seed.py` (`LESSON_AT` 09:30, `CALLOUT_AT` 08:40; moving them also needs a new voice note naming the hour, `scripts/make_preschool_recordings.py` and the fixtures). Hero `L-1109-G002`: Mon 09:30–10:15, V1 teaching pool, "Preschool swim, ages 3–5", 8 children aged 3–5 (norm 1:4, two instructors): S05 Marina Petrova (swim instructor) and S01 Emma Carter on her seat `L-1109-G002-I2`. Marina's voice note at 08:40:10 ("…к половине десятого я не выйду. У меня в полдесятого занятие с малышами…", synthesized locally with `say -v Milena`, `tests/fixtures/audio/ru-callout-0930.ogg`; its transcript, translation, filter and understanding are prepared answers marked `synthetic`). The lesson needs an instructor with WSI, CPR/AED and the school's own preschool training (preset program `preschool`, one level `3-5`; held by S01, S05, S06, S11, S17, S20, S22, S25, S26, S27). 40 → 5 cleared, in order Sofia Reyes, Leo Martins, Zoe Grant, Mateo Romero, Ethan Price (Mateo's Monday 10:00 Maplewood block is on Friday 14:00 in this story); main reasons: not an instructor 13, no preschool training 11, reserve 3, certificates expired 3 (S07 CPR Nov 8, S10 WSI Nov 2, S27 WSI Nov 5), S14 WSI revoked, S13 readers disagree, S11 over 40 h, S01 teaching at 9:30, S05 called out. Head coaches in reserve stay free for work starting before the lesson ends. Anna Kim (S27) is free, volunteers first and is blocked: her WSI ended Nov 5. G121 (Mon 09:30–10:15, lap lanes, Level 4, 6 children, S04) is the group next door: merging breaks 1:4. G003 (Level 1, 4 children aged 4–5, mostly Spanish-speaking families, S01) is at 10:30: enrolling 2 more opens a second-instructor case. Judge runs: `dangerous_morning` — one wave to Sofia, Leo, Zoe; Sofia declines 08:43, Zoe says yes 08:44, Leo (higher) stays silent through the 60 s window, Zoe gets it at 08:45 and Leo stands down; `nobody_accepts` — everyone says no, plan B at 08:45:50 (Liam Foster in reserve, Camila Herrera on overtime 0.25 h $8.50, move, cancel with credit; the deadline would be 09:10); at 08:46:50 Liam texts "A pipe burst at my place, I can't come in today. Sorry." (his four Maplewood lessons open as cases, he is out for the day); at 08:47:10 the shown plan gets 409 and a new plan starts with Camila's overtime (today's timeline: 08:45 Plan B ready · 08:46 Liam Foster called out · 08:47 New plan B ready); `nightly_check` — Sunday 21:30, a card that ends before Monday's shift opens a cover case for approval.

**deck** (tests only, "another morning"): demo clock 05:12; hero `L-1109-G002` is Marina's 06:00–08:00 deck shift at V1 lanes 3–4 + Learn-to-Swim Level 3, 8 children aged 4–5, a lifeguard with WSI, with the recorded Russian voice note `ru-callout.ogg` and the recorded model answers of the 2026-11-06 check; 40 → 5 cleared (Gabriela: only 9 h of rest; Carlos Mendoza: lifeguard card revoked); Anna's lifeguard card ended Nov 5; G003 is at 07:00. `network_loan` — everyone cleared at Riverside says no, Riverside's agent asks Northside's over A2A (two services in compose: `api` and `agent-north`, `COACHCOVER_A2A_PEERS`), Kai Morgan is checked by both venues' rules, Natalie Brooks and Omar Haddad approve (network "Bay Swim Network"). The tests of the pipeline, offer waves, A2A and MCP run on this story.

The judge run waits up to 10 s for Northside's agent card, else 503 "Northside agent is starting, try again in a few seconds"; each agent stamps its steps at least 1 s after the message it answers, so the loan's steps are in time order. `POST /api/standby {shift_id, staff_id}` records a standby proposal (only "propose"; 409 if the person is not cleared). The week is clean: the greedy fill never books a person with an expired, revoked or disputed card or an overdue renewal; the only holder trap is S07's Monday 16:00 lesson (the night check's).
