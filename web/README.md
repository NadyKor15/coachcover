# CoachCover web screens

Static files, no build step. Three pages:

| Page | Screen | Data |
|---|---|---|
| `index.html` | C: manager's morning, "Who is allowed on deck at 06:00" (1920 × 1080) | `GET /api/today` |
| `offer.html?id=O-1` | P2: shift offer on the coach's phone (430 wide) | `GET /api/offers/{id}` |
| `case.html?id=C-1109-01` | case page (proof record, variant B), 1920 wide, height follows the data | `GET /api/cases/{id}` |

Scripts: `assets/api.js` (shared fetch layer + fixture mode), `assets/today.js`, `assets/offer.js`, `assets/case.js`. Styles: `assets/today.css`, `assets/offer.css`, `assets/case.css`. Fonts come from Google Fonts (Inter for C and the case page, Figtree for P2). On screen C the covered line and the timeline open the case of the morning; on the case page "Today" goes back. All data strings are inserted as text, never as HTML.

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

`?fixture=1` reads `fixtures/*.json` instead of the API. The files follow `docs/API-CONTRACT-2026-11-06.md` (training school, fictional names of the contract).

| File | Stands for |
|---|---|
| `fixtures/today.json` | `GET /api/today` after the 5 a.m. call-out (default) |
| `fixtures/today.nobody_accepts.json` | `GET /api/today` after the judge runs "Nobody accepts" |
| `fixtures/offers/O-1.json` | `GET /api/offers/O-1` |
| `fixtures/cases/C-1109-01.json` | `GET /api/cases/C-1109-01`, the case of the covered morning |
| `fixtures/mocks.json` | answers of the POST calls |

POST calls in fixture mode:

- `POST /api/judge/run` answers `{"case_id": …}` per scenario, then the screen re-reads `/api/today`. "Night check" has no fixture of its own and shows the default morning.
- `POST /api/plans/{id}/approve` answers 200 when the hash matches; add `&mock=plan_changed` to get `409 {"reason":"plan changed", …}`.
- `POST /api/offers/{id}/accept|decline` answers 200; add `&mock=not_eligible` to get `409 {"reason": …}` on accept.

## Endpoints the screens use

| Call | Used for | Answers the screens handle |
|---|---|---|
| `GET /api/today[?venue=V1]` | screen C; polled every 5 s, and every 1.5 s for 30 s after a judge run | 200; anything else shows a red line at the top |
| `POST /api/judge/run {"scenario"}` | judge buttons "Run a 5 a.m. call-out" (`dangerous_morning`), "Nobody accepts" (`nobody_accepts`), "Night check" (`nightly_check`) | 200 `{"case_id"}`; 429 and others are shown under the buttons |
| `POST /api/plans/{id}/approve {"hash"}` | "Approve plan B", sends `plan_b.hash` | 200; 409 → "Plan changed since you saw it — not approved" |
| `GET /api/offers/{id}` | screen P2 | 200; anything else shows a red line |
| `POST /api/offers/{id}/accept`, `/decline` | P2 buttons | 200 `{"state"}`; 409 `{"reason"}` → "You are no longer eligible for this shift: <reason>"; other errors keep the buttons |
| `GET /api/cases/{id}` | case page; "Download case (JSON)" saves exactly this answer | 200; anything else shows a red line |
| `GET <callout.audio_url>` | play button of the voice note | audio file; if missing, the button does nothing |

How fields are used, beyond the plain slots:

- `plan_b.state`: `standby` (button off), `proposed` or `pending` (button on), `approved` / `done` / `executed` (shown as approved).
- `timeline[].kind`: `callout`, `declined`, `no_reply`, `accepted`, `plan_b`, `shift_start`; other kinds get a grey mark. A label that starts with a staff name is split into name + result on two lines.
- `offers[].result`: same words as `kind`. The row text after the name comes from the timeline label of the same person and time ("no reply in 5 min"), else from `result`. `why_first` is shown under the accepted offer.
- `eligibility.reasons[].tone`: `red`, `amber` (needs review), anything else plain. Chips split the label before a trailing date or qualifier ("Certificate expired | Nov 5", "Over 40 h | this week"); an optional `detail` field overrides the split.
- `eligibility.first_refusal.label`: shown after the staff name; the word `BLOCKED:` is set in bold when present.
- `money`: every value is optional. `null`, `0` and `"…"` count as unknown and are not shown; with no known value the block is hidden.
- `summary_cards[]`: icon is picked by the title (tomorrow → calendar, expiring/certificate → certificate, closures → pool).

## Case JSON (proposed shape for `GET /api/cases/{id}`)

The contract lists the steps of a case but no fields. The page reads this shape (see `fixtures/cases/C-1109-01.json`):

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
python3 tests/web/make_fixtures.py web/fixtures   # rebuilds the fixtures from one script
PLAYWRIGHT_MODULE=<…/node_modules/playwright> node tests/web/e2e_fixture.js --shots web/shots   # buttons, 200 and 409 paths
python3 tests/web/compare_regions.py web/shots/ref-variant-C.png web/shots/C-fixture-1920.png  # region diff against the approved HTML
```

Snapshots in `shots/`: `C-fixture-1920.png`, `P2-fixture-430.png`, the states after the buttons (`C-fixture-nobody-accepts.png`, `C-fixture-plan-changed-409.png`, `P2-fixture-accepted.png`, `P2-fixture-not-eligible-409.png`), the case page `case-fixture-1920.png`, renders of the approved HTML (`ref-variant-*.png`) and side-by-side pairs (`compare-*.png`).
