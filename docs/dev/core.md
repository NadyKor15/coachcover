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
- A person who called out is offered nothing in their absence window, at least their whole local day (`Context.absences`, loaded by `repo.load_absences` from call-out cases): `CALLED_OUT`, on screen "Called out sick today".
- Groups on one shift are taught together; their norm is the strictest level's `max_per_instructor` times `shift.instructors`; a group whose youngest child is at most `young_children.max_age` uses `young_children.max_per_instructor` (1:4) when that is stricter.
- `shift.extra_certs`: cards this one shift needs on top of the role's (the hero deck shift in the seed also needs the WSI card).
- Revoked cards (idea 2): `issuer_registry` (SIMULATED in the demo: training data, `simulated = true`, the screen label says "(simulated registry)") is matched by card type and number without spaces/dashes. A revoked card stops at 00:00 of its revocation day; when the printed dates would have covered the shift, the reason is `CERT_REVOKED` ("Valid until Jun 2028 — REVOKED by issuer on Jul 24"). Seed: Carlos Mendoza (S35), revoked 2026-07-24.
- Rest (idea 3): preset `min_rest_hours` (10). Work on an earlier local day must end, and work on a later day must start, at least that far from the shift: `REST_TOO_SHORT` ("Only 9 h rest since 21:00" / "... before 6:00"). Work on the shift's own start day is one working day (a split shift), not a rest question. Seed: Gabriela Vargas closes Lakeview on Sunday at 21:00, so the hero morning is 40 → 5; the greedy fill never breaks the rule.
- Queue order (product decision): eligible; fewer hours this week; same venue, then distance; program and level match; longer tenure; then (idea 4) speaks the language of more than half of the group's families (`lesson_group.family_counts`, `staff.languages`); staff id. `Ranked.place()` is the machine reason; `describe_place` gives the line "Maria first: 12 h this week, Level 3, same pool".

## Pipeline

- Call-out: voice → `whisper-large-v3-turbo` → text → `muse-glimmer-30b` (`extract_callout` schema) → shifts of today at the named start times (none understood: all of today's remaining shifts) → one case per shift.
- Offers (product decision) go in waves of the preset's `offers.wave_size` (3) to the next eligible people in queue order (`decision.ok`), reserve roles never; the timer is `offer_timeout_minutes`. A "yes" is `held` while someone higher in the wave can still answer, at most `offers.collect_seconds` (60) after the first "yes"; then the highest held "yes" gets the shift (the rules run again) and every other open offer stands down ("thanks — Maria already covered …"). A "yes" after that is a 409 with the reason "Thanks — Maria already covered it". A wave nobody answers in time is followed by the next one. The worker's tick (`expire_due`) closes windows and sends waves. The "why" line names the place in the wave ("Maria third: …").
- Plan B by the deadline: `offers.plan_b_minutes_before_start` (20) before the start, an open case gets plan B even while offers are out (event `plan_b_deadline`); a later "yes" still covers the shift and the proposed plan becomes `superseded`.
- Autonomy per action: `auto` acts, `propose` leaves a card (`approval` for assignments, `plan_b` row for plan B), `off` does nothing. An option's mode is the strictest of `plan_b` and its own action (`merge_groups`, `overtime`); overtime is never automatic.
- Plan B options, computed by code in this order: merge into a concurrent group within the norm, a reserve person steps in, overtime (price = hours over × `overtime_cost_per_hour`), move (makeup), close (credit or makeup). The text model only writes the lines. Approval needs the shown snapshot hash; if the facts changed since, the plan is superseded and the call fails with 409 and the new plan.
- Plan B never suggests a person already asked in the case (declined, no reply, withdrawn), not even as overtime. An approved option that cannot be carried out marks the plan `failed` (event `plan_b_failed`) and a new plan is proposed.
- Message filter: every call-out and volunteer message goes through `nemotron-3.5-content-safety`; it only flags (`flagged[]`, event `message_flagged`), the rules still decide. Anna's "just assign me anyway" is flagged and still blocked by her expired card.
- Night check (`night_cover` action, `propose` by default): a shift of tomorrow whose holder fails the rules gets a cover case with an approval card.
- Live norm (idea 1): `POST /api/groups/{id}/enroll {add, language?, by}` grows the group and re-checks its shifts of the coming week. A shift over the norm gets the missing instructor seats as shifts teaching the same groups at the same time (`extra_for`, id `<shift>-I<n>`, `instructors` raised on all of them) and a cover case each (`source = 'ratio'`, event `ratio_broken`); the same queue fills them. `/api/today.ratio_alert`: red while open, green "Covered by …" once filled. Plan B of a ratio case (`planb.ratio_plan_options`) never leaves a lesson over the norm: (a) a reserve person takes the seat; (b) `split` — another cleared instructor teaching at the same time takes the newest children as a group of their own level (a new `lesson_group` `<group>-S<case>` on that shift); (c) `move` — the newest children (by `group_enrollment`) go to the nearest later lesson of their level with room, families told in their language; with none of these, `cancel_lesson` cancels the lesson and its seats with credits. After (b)/(c) the open seats close and the lesson keeps the instructors it has. A case closed by plan B withdraws its open offers. `tests/test_ratio_plan_b.py` checks the invariant over all 120 groups of the training school and every option offered (228 applied).
- Family messages: one model-written template per language with `{group}`/`{time}`, filled per group; template text if no model answer. Channel is always `simulated`.
- Cards: two readers (muse, omni). Disagreement on type or dates, a failed reader, an unknown type or another person's name → flags → `needs_review`; readers that agree on everything but the card number → trusted with the note `check_id`. The database recomputes trust from the readings and the stored dates.
- Model replay: recordings are found by (purpose, model, sha256 of the input key). Real recordings come from the 2026-11-06 check; synthetic ones say `"synthetic": true` and their source. The transcript recordings add the `model` field that the check did not keep: verify it live.

## API

Shapes: `docs/API-CONTRACT-2026-11-06.md` and `web/fixtures/*.json`; `tests/test_contract.py` checks every fixture key and type against the live answers.

`GET /api/today?venue=` · `POST /api/callouts` (multipart: `staff_id` + `audio` or `text`) · `GET /api/cases/{C-MMDD-NN}` (`?download=true`) · `POST /api/cases/{id}/volunteer` · `GET /api/offers/{O-n}` · `POST /api/offers/{O-n}/accept|decline` (409 `{"reason"}` when the rules refuse the yes) · `POST /api/plans/{P-n}/approve` (`hash`, optional `option`; 409 `{"reason": "plan changed", "current_hash"}`) · `POST /api/approvals/{id}/approve|reject` · `POST /api/closures` · `POST /api/staff/{id}/certificates` (photo, optional `cert_type`) · `POST /api/import` · `POST /api/groups/{id}/enroll` · `GET /api/stats` · `GET /api/models/ledger` · `GET /media/callouts/{code}.ogg` · `POST /api/judge/run {"scenario"}` (`dangerous_morning`, `nobody_accepts`, `nightly_check`; resets the school, `COACHCOVER_JUDGE_RUNS_PER_HOUR` per client address, the limit lives in `ops` and survives the reset) · `GET /health` (`mode`), plus the extension router (`coachcover/ext_api.py`, `docs/dev/ext.md`: `/api/week`, `/api/network/…`, `/a2a/{venue}`, `/.well-known/agent-card.json`, `/api/whatif`, `/api/binder.pdf`, `/api/team-posts`, `/mcp`), included before the static mount. The screens in `web/` are served from `/`, plus `/offer` and `/case`. `money.source` on `/api/today` is the lesson price's source; model costs print through `money.cost_label` (dollars from one cent, else cents).

The case page and today's screen are flat fields plus arrays (`timeline[].line`, `filter_chips[]`, `filter_grid[]`, `offers[]`, `plans[].options[]`, `model_calls[]`, `expiring_30[]`...) for `data-slot` filling.

## Schedule import (`POST /api/import`)

"We don't replace your schedule — we add the certification layer on top of it."

Multipart: `file` (CSV or iCal, ≤ 2 MB), optional `tz` (default: the venues' zone), `date_order` (`MDY`/`DMY`), `venue_id` (for rows without a known place), `date_from` + `date_to` (bounds repeating iCal events). Places are matched by venue name, people by exact name, positions by role label. Bad rows are named (`errors`: line, field, why) and skipped; good rows are saved as shifts with stable ids `IMP-…` (re-importing the same file saves nothing new: `already_there`). A row with nobody, or with a name not on the staff list, is saved `open`, so the night check sees the gap. A row that would book a person twice at the same time is not saved (`not_saved`) and its check names the overlap. Every assigned row is checked at the demo clock against the rules over the imported schedule plus the stored one: `checks[]` with `ok`, `blocking[]`, `warnings[]` (rule codes), `line`, `name`, `saved`. A blocked row is still saved: it is the school's schedule, and the night check keeps flagging it.

## Worker

`python -m coachcover.worker`: every tick expires offers whose timer ran out (the next person is asked) and, after `COACHCOVER_NIGHT_CHECK_HOUR` (21) venue time, writes tomorrow's report once. All its work is database state, so a restart loses nothing. One log line per tick; exits after too many failures in a row.

## Training school (`coachcover/seed.py`)

"Today" is Mon 2026-11-09 05:12 America/New_York (demo clock). Lesson price $30 per child (founder, a product decision): the uncovered hero morning is 8 × $30 = $240. Riverside Aquatic Center (V1), Maplewood Swim Center (V2), Lakeview Natatorium (V3); manager Dana Ruiz; 40 people. Hero shift `L-1109-G002`: 06:00–08:00 at V1 lanes 3–4, level 3, 8 children aged 4–5 (norm 1:4, two instructors), held by S05 Marina Petrova, who calls out (recorded Russian voice note). 40 people → 5 cleared, in order Sofia Reyes, Leo Martins, Maria Lopez, Mateo Romero, Ethan Price (Gabriela Vargas: only 9 h of rest; Carlos Mendoza: lifeguard card revoked, simulated registry). G003 (Mon 07:00, Level 1, 4 children aged 4–5, mostly Spanish-speaking families) is at its 1:4 norm: enrolling 2 more opens a second-instructor case; the first offer goes to Sofía García, "speaks Spanish like 5 of 6 families". Anna Kim (S27) volunteers first and is blocked: her lifeguard card ended Nov 5. Traps: S07 CPR ends Nov 8, S10 WSI Nov 2, S08 CPR and S30 lifeguard on Nov 9 (warning), S13 readers disagree, S14 renewal overdue. Judge runs: `dangerous_morning` — one wave to Sofia, Leo, Maria; Sofia declines 05:13, Maria says yes 05:14, Leo (higher) stays silent through the 60 s window, Maria gets it at 05:15 and Leo stands down; `nobody_accepts` — everyone says no, plan B is shown, then a closure changes the facts and the shown plan gets 409; `nightly_check` — Sunday 21:30, a card that ends before Monday's shift opens a cover case for approval; `network_loan` — everyone cleared at Riverside says no, Riverside's agent asks Northside's over A2A (two services in compose: `api` and `agent-north`, `COACHCOVER_A2A_PEERS`), Kai Morgan is checked by both venues' rules, Dana Ruiz and Omar Haddad approve. The week is clean: the greedy fill never books a person with an expired, revoked or disputed card or an overdue renewal; the only holder trap is S07's Monday 16:00 lesson (the night check's).
