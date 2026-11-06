# CoachCover web screens

Static files, no build step. Three pages:

| Page | Screen | Data |
|---|---|---|
| `index.html` | C: manager's morning, "Who is allowed on deck at 06:00" (1920 × 1080) | `GET /api/today` |
| `offer.html?id=O-1` | P2: shift offer on the coach's phone (430 wide) | `GET /api/offers/{id}` |
| `case.html?id=C-…` | case page: placeholder until its design is approved; shows the case JSON and a download link | `GET /api/cases/{id}` |

Scripts: `assets/api.js` (shared fetch layer + fixture mode), `assets/today.js`, `assets/offer.js`. Styles: `assets/today.css`, `assets/offer.css`. Fonts come from Google Fonts (Inter for C, Figtree for P2). All data strings are inserted as text, never as HTML.

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
| `GET /api/cases/{id}` | case placeholder | 200 → JSON + download |
| `GET <callout.audio_url>` | play button of the voice note | audio file; if missing, the button does nothing |

How fields are used, beyond the plain slots:

- `plan_b.state`: `standby` (button off), `proposed` or `pending` (button on), `approved` / `done` / `executed` (shown as approved).
- `timeline[].kind`: `callout`, `declined`, `no_reply`, `accepted`, `plan_b`, `shift_start`; other kinds get a grey mark. A label that starts with a staff name is split into name + result on two lines.
- `offers[].result`: same words as `kind`. The row text after the name comes from the timeline label of the same person and time ("no reply in 5 min"), else from `result`. `why_first` is shown under the accepted offer.
- `eligibility.reasons[].tone`: `red`, `amber` (needs review), anything else plain. Chips split the label before a trailing date or qualifier ("Certificate expired | Nov 5", "Over 40 h | this week"); an optional `detail` field overrides the split.
- `eligibility.first_refusal.label`: shown after the staff name; the word `BLOCKED:` is set in bold when present.
- `money`: every value is optional. `null`, `0` and `"…"` count as unknown and are not shown; with no known value the block is hidden.
- `summary_cards[]`: icon is picked by the title (tomorrow → calendar, expiring/certificate → certificate, closures → pool).

## Checks

```bash
python3 -m pytest tests/web                      # fixtures follow the contract and tell one story
PLAYWRIGHT_MODULE=<…/node_modules/playwright> node tests/web/e2e_fixture.js --shots web/shots   # buttons, 200 and 409 paths
python3 tests/web/compare_regions.py web/shots/ref-variant-C.png web/shots/C-fixture-1920.png  # region diff against the approved HTML
```

Snapshots in `shots/`: `C-fixture-1920.png`, `P2-fixture-430.png`, the states after the buttons (`C-fixture-nobody-accepts.png`, `C-fixture-plan-changed-409.png`, `P2-fixture-accepted.png`, `P2-fixture-not-eligible-409.png`), renders of the approved HTML (`ref-variant-*.png`) and side-by-side pairs (`compare-*.png`).
