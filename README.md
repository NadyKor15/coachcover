# CoachCover

When a swim coach writes in the morning that they can't come in, CoachCover finds a replacement who is allowed to teach
that lesson, asks them, and keeps a record of why each person was or wasn't asked.

## Judge it in 90 seconds

Live demo: https://216-155-135-169.sslip.io

The morning screen has a Judge mode panel on the left. Each button resets the demo school and plays one scene; the
model calls in it go to the models on Vultr. A visitor can run 6 scenes an hour.

| Press | What you see |
|---|---|
| Run the call-out | 08:40, Marina's voice note in Russian: she has a fever and can't teach her 09:30 class of 3–5-year-olds. The note is transcribed and translated, 40 staff are checked and 5 are allowed. Anna Kim offers herself and is blocked because her WSI card expired on Nov 5. Three allowed coaches get the offer, and Zoe Grant takes the class at 08:45. |
| Nobody accepts | Everyone asked says no or doesn't answer, and plan B waits for your approval. At 08:46 the reserve head coach that plan starts with calls out too, so the first plan is refused ("plan changed") and a new plan waits for approval. |
| Network (after Nobody accepts) | Open Network and press Ask the network. Riverside's agent asks Northside's agent over A2A and Kai Morgan is checked against both venues' rules. Once each manager approves on their own card, Kai covers the 09:30 class. |
| Night check | Sunday 21:30: Javier Morales's CPR/AED card ends Nov 8, before his Monday 16:00 shift. The case opens the night before and asks the manager to approve finding a replacement. |
| Private lesson: yes | 12:10: Marina can't teach her 17:00 private lesson with Lily. Chloe Hayes says yes, Lily's parent is asked and agrees, and only then is Chloe booked. |
| Private lesson: no | The parent says no. Chloe is released, and the lesson moves to Marina's next free 17:00 slot (Wednesday) once the parent agrees. |

Click the covered line or the timeline to open the case page. It lists every step, the rule behind each refusal,
and every model call with its time and cost. "Download case (JSON)" saves the same record.

"Inspector's folder (PDF)", on Today under the cards on the right and on Week next to the title, opens the folder for
the week that still has shifts ahead: each post with the person on it and their cards valid that day, the replacements
and the closures, with hashes.

## The use case

The founder coached swimming for more than ten years, in fitness centers and in her own practice. Coaches often wrote
early in the morning to say they couldn't make the first lesson, and the manager then had to find, before it started,
someone with the right certificates who was cleared for that program.

## How it runs on Vultr

CoachCover runs on Vultr: one VM and Serverless Inference.

One Vultr Cloud Compute VM runs the whole app in Docker Compose. That covers the API and the screens, the worker
(offer timers, the family's answer window, the night check), the MCP server for assistants, two A2A agents (Riverside's
inside the app, Northside's in its own container), Postgres, and Caddy for HTTPS.

Vultr Serverless Inference runs four models:

| Model | What it does here |
|---|---|
| `whisper-large-v3-turbo` | turns the voice note into text |
| `muse-glimmer-30b` | reads the call-out (whether they're out, which lessons, why), translates it, writes plan B and the questions to families, and gives the first reading of each certificate card |
| `nemotron-3-nano-omni-30b-a3b-reasoning` | reads each certificate card a second time, on its own; if the two readings disagree on the dates, a person checks the card |
| `nemotron-3.5-content-safety` | checks each message from staff; a flagged message goes to the manager, and the rules still apply |

Nemotron 3 Nano Omni is NVIDIA's model. Its [model card](https://huggingface.co/nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-BF16)
says it was improved using several Qwen models and gpt-oss-120b.

## Rules decide, models don't

Whether a person may take a lesson is decided in code (`coachcover/rules.py`): role, each required certificate and its
dates, program and level, weekly hours, rest between days, overlapping bookings and the children-per-instructor ratio.
The models turn speech into text, translate, read cards and write text. A plan B or a question to a family written by a
model is checked against the facts first (names, times, money); if the check fails, a template built from the same facts
is shown instead.

Offers go only to people the rules allow, three at a time, and the rules run again when someone says yes. A private
lesson goes to another coach only after the child's parent says yes. Plan B (merge with the next group, the reserve
coach, overtime, move the lesson, cancel) is approved by the manager against a hash of the facts it was built from: if
those facts change, the old plan is refused.

## Measured

`docs/sim/SIM-50-MORNINGS.md` replays 50 synthetic mornings (seed 20261109) with a group chat plus phone tree and with
CoachCover, on the same school and the same call-outs:

| | Group chat + phone tree | CoachCover |
|---|---:|---:|
| Shifts that needed cover | 98 | 98 |
| Placements of someone not cleared for the shift | 45 | 0 |
| Shifts closed after they had started | 0 | 0 |
| Private lessons given to another coach without asking the family | 11 of 11 | 0 |
| Median minutes from call-out to a closed shift | 13.5 | 10.5 |
| 90th percentile, minutes | 16.3 | 25.0 |

Of the 11 private lessons, CoachCover booked another coach 8 times after the family said yes, moved 2 to the family's
own coach and gave 1 credit. How families answer is assumed, not measured (yes 80%, no 10%, no answer 10%).

The slowest tenth of shifts close later with CoachCover (25.0 vs 16.3 minutes). It asks only people who are allowed,
three at a time with a 5-minute timer, and plan B waits for the manager; the chat books whoever answers first.

In a live run on Vultr (Nov 7), the morning case took 5 model calls for 0.113¢, about a tenth of a cent.

In an earlier build we tried growing waves (3, then 5, then everyone): no faster in the slowest mornings, and more people
were asked for nothing, so we kept waves of 3.

## Replay and live

The live demo calls the models on Vultr. By default a local copy runs in replay mode instead: 91 answers recorded from
the Vultr models and 31 written by hand, each of those marked `"synthetic": true` (all in `tests/fixtures/models`). The
footer of every screen follows the server's mode: "CoachCover · Running on Vultr." when it calls the models, "CoachCover
· Replay mode." when it replays them. The case page also marks each model call live, recorded or prepared.

## What's not here

1. Before a coach is lent to another venue they would get an orientation there; the demo has no step for it.
2. The inspector's folder has the staff on each post with their clearances, the cover cases and the closures.
   In-service training records, the emergency action plan and water quality logs are not in it.
3. Who may teach the 3–5-year-olds is this school's own rule (its preschool training), not an industry standard.
4. Waves of three and the evening check are product choices, not common practice.
5. The demo data is made up: one school, one week.

## Prices

The money on the screens uses $30 per child for a group lesson and $50 for a 30-minute private lesson, from the
founder's coaching practice.

## Run it yourself

```bash
git clone https://github.com/NadyKor15/coachcover && cd coachcover
POSTGRES_PASSWORD=$(openssl rand -hex 16) docker compose up -d --build
```

Open http://127.0.0.1:8088. It starts in replay mode and loads the demo school on a fresh database. For live models set
`COACHCOVER_MODEL_MODE=live` and pass `VULTR_INFERENCE_API_KEY` in the environment of the command, never in a file.
To put it on a Vultr VM with HTTPS, see [deploy/README.md](deploy/README.md).

Tests on this commit: 415 pytest tests (the database tests need a Postgres, `COACHCOVER_TEST_DSN`) and 328 browser
checks (`tests/web/e2e_fixture.js`).

License: MIT.
