# CoachCover web screens

Static files, no build step. Three pages:

| Page | Screen | Data |
|---|---|---|
| `index.html` | C: manager's morning, "Who is allowed on deck at 09:30" (1920 × 1080) | `GET /api/today` |
| `offer.html?id=O-1` | P2: shift offer on the coach's phone (430 wide) | `GET /api/offers/{id}` |
| `client.html?id=Q-1` | the family's page of a private lesson: another coach or a new time, Yes / No (430 wide; `&mock=client_declined`, `client_credit`, `client_closed` in fixture mode) | `GET /api/client/{id}`, `POST /api/client/{id}/respond` |
| `case.html?id=C-1109-01` | case page (proof record, variant B), 1920 wide, height follows the data | `GET /api/cases/{id}` |
| `week.html[?venue=V1&start=2026-11-08]` | Week ahead (W_B): backups per shift, fragile shifts, plan ahead | `GET /api/week`, `GET /api/today` |
| `network.html?loan=LN-1109-01` | Network (N_B): two venue agents over A2A, one approval per manager; without `loan` it offers to ask the network for today's open shift | `GET /api/network/loans/{ref}`, `GET /api/network`, `GET /api/today` |

Scripts: `assets/api.js` (shared fetch layer + fixture mode), `assets/today.js`, `assets/offer.js`, `assets/case.js`, `assets/week.js`, `assets/network.js`. Styles: `assets/today.css`, `assets/offer.css`, `assets/case.css`, `assets/week.css`, `assets/network.css`, and `assets/status.css`: the status colour tokens `--st-{safe|fragile|risk}-{bg,ink,line,dot,hatch}` (palette A; palette B's values are in its header comment, choosing B changes only that file). Fonts come from Google Fonts (Inter for C and the case page, Figtree for P2). On screen C the covered line and the timeline open the case of the morning; on the case page "Today" goes back. All data strings are inserted as text, never as HTML.

## Serving

The pages call `/api/...` on the same origin, so serve `web/` and the API from one host.

FastAPI (API routes first, static last, so `/api/*` wins):

```python
from fastapi.staticfiles import StaticFiles
app.mount("/", StaticFiles(directory="web", html=True), name="web")
```

Caddy:

```
handle /api/* { reverse_proxy api:8000 }
handle /media/* { reverse_proxy api:8000 }
handle { root * /srv/web; file_server }
```

Without the API (fixture mode, nothing leaves the browser):

```bash
python3 -m http.server 8080 --bind 127.0.0.1 -d web
# http://127.0.0.1:8080/index.html?fixture=1
# http://127.0.0.1:8080/offer.html?id=O-1&fixture=1
```

`file://` does not work: browsers block `fetch` of local files.

## Fixture mode

`?fixture=1` reads `fixtures/*.json` instead of the API. Every file is an answer of the running API, captured by `tests/web/make_fixtures.py` (replay mode, training school), so the fixtures and the live screens tell the same story.

```bash
POSTGRES_PASSWORD=$(openssl rand -hex 16) API_PORT=18088 COACHCOVER_JUDGE_RUNS_PER_HOUR=60 \
  docker compose -p ccweb-fx up -d --build          # own project name: own db, network and volume
python3 tests/web/make_fixtures.py http://127.0.0.1:18088 web/fixtures
docker compose -p ccweb-fx down -v --rmi local
```

| File | Stands for |
|---|---|
| `fixtures/today.json` | `GET /api/today` after the judge runs the call-out (default) |
| `fixtures/today.nobody_accepts.json`, `today.nightly_check.json` | `GET /api/today` after the other two judge runs |
| `fixtures/today.ratio_open.json`, `today.ratio_covered.json` | `GET /api/today` after 2 children are enrolled into G003 (Mon 10:30): ratio banner red, then green |
| `fixtures/offers/O-*.json` | `GET /api/offers/{id}`: every offer of the morning, plus one open offer for the phone screen |
| `fixtures/cases/C-*.json` | `GET /api/cases/{id}`: the morning's case and the ratio case |
| `fixtures/mocks.json` | the real answers of the POST calls, captured in the same run |
| `fixtures/week.json` | `GET /api/week?venue=V1&start=2026-11-08` on the morning of the main story |
| `fixtures/loans/*.json`, `network.json`, `network_mocks.json` | a loan in every state (candidate, one approval either side, approved by both from the judge's `network_loan` scene, declined by Northside to keep its own guards, rejected by Riverside's rules) and the real 200 / 403 / 409 answers of `approve` and of asking the network |
| `fixtures/today.network_open.json`, `today.network_assigned.json`, `cases/C-1109-01.network_loan.json` | the open shift before the loan, the morning after it |

POST calls and switches in fixture mode:

- `POST /api/judge/run` answers what the API answered for that scenario, then the screen re-reads `/api/today`.
- `POST /api/plans/{id}/approve` answers 200 when the hash matches; `&mock=plan_changed` gives the real `409 {"reason":"plan changed", …}`.
- `POST /api/offers/{id}/accept|decline` answers the real 200; `&mock=not_eligible` gives the real 409 on accept.
- `&mock=ratio_open` / `&mock=ratio_covered` show the ratio banner states.
- Network: `&mock=<state>` opens the loan in that state; the two approve buttons move it on (Riverside first or Northside first), `&mock=approve_conflict` gives the 409; `&mock=network_open` is the shift before the loan, with `ask_conflict` asking gets the 409.

## Endpoints the screens use

| Call | Used for | Answers the screens handle |
|---|---|---|
| `GET /api/today[?venue=V1]` | screen C; polled every 5 s, and every 1.5 s for 30 s after a judge run | 200; anything else shows a red line at the top |
| `POST /api/judge/run {"scenario"}` | judge buttons "Run the call-out" (`dangerous_morning`), "Nobody accepts" (`nobody_accepts`), "Night check" (`nightly_check`) | 200 `{"case_id"}`; 429 and others are shown under the buttons |
| `POST /api/plans/{id}/approve {"hash"}` | "Approve plan B", sends `plan_b.hash` | 200; 409 → "Plan changed since you saw it — not approved" |
| `GET /api/offers/{id}` | screen P2 | 200; anything else shows a red line |
| `POST /api/offers/{id}/accept`, `/decline` | P2 buttons | 200 `{"state"}`; 409 `{"reason"}` → "You are no longer eligible for this shift: <reason>"; other errors keep the buttons |
| `GET /api/cases/{id}` | case page; "Download case (JSON)" saves exactly this answer | 200; anything else shows a red line |
| `GET /api/week[?venue&start]` | Week ahead | 200; anything else shows a red line |
| `GET /api/network/loans/{ref}`, `GET /api/network` | Network | 200; anything else shows a red line |
| `POST /api/network/loans/{ref}/approve {manager, venue}` | one button per manager, each for their own venue | 200 reloads the loan; 403 and 409 show in that manager's card |
| `POST /api/network/loans {shift_id}` | "Ask the network" for today's open shift | 201 opens the loan; 409 is shown |
| `GET <callout.audio_url>` | play button of the voice note | audio file; if missing, the button does nothing |

How fields are used, beyond the plain slots:

- `plan_b.state`: `standby` (button off), `proposed` or `pending` (button on), `approved` / `done` / `executed` (shown as approved).
- `timeline[].kind`: `callout`, `declined`, `no_reply`, `accepted`, `plan_b`, `shift_start`; other kinds get a grey mark. A label that starts with a staff name is split into name + result on two lines.
- `offers[].result`: same words as `kind`. The row text after the name comes from the timeline label of the same person and time ("no reply in 5 min"), else from `result`. `why_first` is shown under the accepted offer.
- `eligibility.reasons[].tone`: `red`, `amber` (needs review), anything else plain.
- `eligibility.first_refusal.label`: shown after the staff name; the word `BLOCKED:` is set in bold when present.
- `money`: `uncovered_morning_label` "If nobody had accepted: $240 (Refunds: 8 participants × $30)" is shown as title, sum and detail; the line under it is the lesson price source (`money.source_label` when the API has it, else `risk.assumptions.lesson_price.source` of the morning's case, without the internal "(TG …)" reference); "This cover: … in model calls" from `case_cost_cents`. `null`, `0` and `"…"` count as unknown; with no known value the block is hidden.
- `ratio_alert`: banner right of the title, red while the seat is open, green once covered (`label · action_label`); a click opens that case. Without a ratio case the same place shows `night_check.label` in amber.
- `eligibility.reasons`: one-line chips in up to 3 rows; chips with the same code and the same first words become one chip ("Certificate expired" ×3, exact labels in the tooltip and on the case page); what does not fit folds into "+N more".
- `offers[].why_first`: shown under the accepted offer with the key "why <first name>".
- `outcome`: the banner names the shift's start (from `shift.time_label`); `covered_time` (when the case closed) is shown small as "closed 05:15".
- Week: rows are the start times of the week's shifts, columns its days; a cell with several shifts shows the weakest; cell level, threshold and legend labels come from the answer (`level`, `threshold`, `legend`, `past_legend`); the fragile list follows `fragile`; "Propose standby" says it is not sent while the API has no standby route.
- Network: the cards are the agents' messages (`request_sent`, `offered`, `checked_offer`, refusals), each with its own UTC stamp; the other steps stay in the record. The result banner is the loan's `status_label` (with the person and case once assigned, with the reason once declined or rejected).
- `summary_cards[]`: icon is picked by the title (tomorrow → calendar, expiring/certificate → certificate, closures → pool).

## Case JSON (`GET /api/cases/{id}`)

The page reads these fields (see `fixtures/cases/C-1109-01.json`); kinds it has no layout for (`flagged`, `night_check`, `ratio`) show their title and summary:

- top: `case_id`, `status` (`covered` gives the gold pill), `status_label`, `covered_time_label`, `shift_label`, `venue{id,name}`, `date_label`, `user_label`, `opened_at`, `closed_at` (UTC);
- `steps[]` in time order, each `{kind, at (UTC ISO), time_label ("10:12:04 UTC"), title}` plus by kind:
  - `callout`: `staff`, `language`, `audio_url`, `transcript_original`, `transcript_en`, `model_tag` ("whisper-large-v3-turbo · 1.0 s");
  - `shift`: `shift_id`, `summary`, `model_tag`;
  - `allowed`: `total`, `allowed`, `blocked{staff,label}`, `reasons[{code,label,count}]`, `staff[{id,name,allowed,reason_label}]` (all 40);
  - `offers`: `offers[{id,time_label,staff,result,result_label,why_first}]`;
  - `covered` / `plan_b`: `summary`;
- `proof{plan_id, plan_hash, policy_label}`; the page shows the hash as `7f3a…c2e1` and the full hash on hover;
- `model_calls[{at, purpose, model (id from the answer), model_label, ms, input_tokens, output_tokens, cost_usd, source}]`, `model_cost_usd`; the table counts calls per `model_label`;
- `risk{kids, refunds_usd, overtime_usd, total_usd}`: the "If nobody had accepted" card shows only the sums that are numbers above 0 and is hidden when none is.

## Checks

```bash
python3 -m pytest tests/web                      # fixtures follow the contract and tell one story
PLAYWRIGHT_MODULE=<…/node_modules/playwright> node tests/web/e2e_fixture.js --shots web/shots   # buttons, 200 and 409 paths
python3 tests/web/compare_regions.py web/shots/ref-variant-C.png web/shots/C-fixture-1920.png  # region diff against the approved HTML
```

Snapshots in `shots/`: `C-fixture-1920.png`, `P2-fixture-430.png`, the states after the buttons (`C-fixture-nobody-accepts.png`, `C-fixture-plan-changed-409.png`, `P2-fixture-accepted.png`, `P2-fixture-not-eligible-409.png`), the case page `case-fixture-1920.png`, renders of the approved HTML (`ref-variant-*.png`) and side-by-side pairs (`compare-*.png`).
