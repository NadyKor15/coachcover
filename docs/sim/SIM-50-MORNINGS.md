# 50 training mornings: group chat + phone tree vs CoachCover

**synthetic school, 50 mornings, seed 20261109.** Every number below comes from one run of `coachcover/sim.py`; nothing is typed by hand.
Repeat: `uv run python -m coachcover.sim --mornings 50 --seed 20261109 --out docs/sim` (same seed, same numbers).

The school is fictional (`coachcover/seed.py`): 3 venues, 40 staff, 205 shifts in the week of 2026-11-09, time zone America/New_York. Each morning is a weekday of that week with 1-3 call-outs before 06:00 and a changed staff state; both strategies run on the same morning.

## Result

| Metric | Group chat + phone tree | CoachCover |
|---|---:|---:|
| Shifts that needed cover | 90 | 90 |
| Placements of a person without permission | 49 | 0 |
|   of them: no role for the shift | 9 | 0 |
|   of them: certificate expired, missing or unconfirmed | 21 | 0 |
|   of them: no program or level, renewal overdue | 9 | 0 |
|   of them: over the weekly hours limit | 10 | 0 |
|   of them: group norm broken | 0 | 0 |
| Placements with an expired certificate (any reason list) | 16 | 0 |
| People placed (cover, merge, reserve, overtime) | 90 | 90 |
| Median minutes from call-out to a closed shift | 13.3 | 10.1 |
| 90th percentile, minutes | 15.7 | 22.0 |
| Closed after the shift had started | 0 | 0 |
| Closed without a person (no manager step), % | 0.0 | 62.2 |
| Lessons cancelled or moved | 0 | 0 |
| Participants left without their lesson | 0 | 0 |
| Shifts without groups left empty | 0 | 0 |
| Approved overtime | 0 | 0 |
| Manager minutes, median per morning | 6.0 | 1.0 |

Outcomes, baseline: {'covered': 90}; CoachCover: {'covered': 56, 'reserve': 34}.
Blocking codes behind the baseline violations: {'CERT_EXPIRED': 17, 'CERT_MISSING': 9, 'CERT_NEEDS_REVIEW': 4, 'CERT_REVOKED': 3, 'HOURS_LIMIT_EXCEEDED': 3, 'LEVEL_MISSING': 13, 'PROGRAM_MISSING': 9, 'PROGRAM_RENEWAL_OVERDUE': 2, 'REST_TOO_SHORT': 14, 'ROLE_MISSING': 9}.

A placement is one person put on a shift (cover, the teacher of a merged group, a reserve person, approved overtime). It is a violation when `rules.eligible` returns a blocking reason for that person and shift at the moment of booking. The breakdown counts each violation once, under the first of: role, certificate, program, hours, group norm. CoachCover's approved overtime is not counted as a violation: the manager approved it with its price, as the core does.

## The two strategies

- **Group chat + phone tree.** The manager sees the call-out after 10.0 min, posts in the staff chat (2.0 min per post) and books the first "yes" that comes before the shift starts (+1.0 min). After 10.0 min without a "yes" the manager also phones free people who hold the shift's role, in a random order, 3.0 min per call. Cards, programs, weekly hours and the group norm are not checked: that is how a chat and a general scheduler work. Nobody by the start: a lesson goes into the group next to it without checking the norm, otherwise it is cancelled; a shift without groups stays empty.
- **CoachCover.** 1.0 min after the call-out the case opens. The rules rank every non-reserve person; offers go to eligible people only, in waves of 3 in queue order, with the policy's timer (5 min); the highest "yes" wins once nobody above it can still answer, at most 60 s after the first "yes"; the rules run again at "yes". Plan B goes to the manager 20 min before the shift starts even while offers are out (a "yes" before the manager acts still covers the shift), or earlier when nobody is left, in the core's order: merge into the group next to it within the norm, a reserve person, overtime (an offer to a person blocked only by hours), move the lesson, close. Plan B is `propose` in the policy, so the manager approves it (10.0 min to see the card, 1.0 min to decide). If an overtime offer is not taken, the manager goes to the next option.

## Before / after: offer waves

CoachCover on the same 50 mornings, before (one offer at a time) and after (waves + plan B by the deadline). The baseline does not change.

| Metric | Before: one offer at a time, plan B when the queue is empty | After: waves of 3, highest yes in 60 s, plan B 20 min before the start |
|---|---:|---:|
| Shifts that needed cover | 90 | 90 |
| People placed | 90 | 90 |
| Placements that break a rule | 0 | 0 |
| Placements with an expired card | 0 | 0 |
| Minutes from call-out to closed, median | 20.4 | 10.1 |
| Minutes from call-out to closed, p90 | 40.8 | 22.0 |
| Minutes from call-out to closed, mean | 21.9 | 12.0 |
| Closed after the shift started | 9 | 0 |
| Closed without a person | 58 | 56 |
| Closed without a person, % | 64.4 | 62.2 |
| Shifts covered (offer or plan B) | 90 | 90 |
| Lessons cancelled or moved | 0 | 0 |
| Children without their lesson | 0 | 0 |
| Shifts without groups left empty | 0 | 0 |
| Approved overtime | 0 | 0 |
| Offers sent (people asked) | 372 | 440 |
| Manager minutes, total | 32.0 | 34.0 |

Outcomes before: {'covered': 58, 'reserve': 32}; after: {'covered': 56, 'reserve': 34}.

## What if (not the main table)

| Metric | `baseline_manager_checks_role` | `coachcover_one_offer_at_a_time` |
|---|---:|---:|
| Placements without permission | 44 | 0 |
|   no role / certificate / program / hours / group norm | 0 / 23 / 11 / 10 / 0 | 0 / 0 / 0 / 0 / 0 |
| Median minutes to close | 13.4 | 20.4 |
| 90th percentile, minutes | 15.7 | 40.8 |
| Closed after the shift had started | 0 | 9 |
| Closed without a person, % | 0.0 | 64.4 |

- `baseline_manager_checks_role`: Baseline where the manager takes only people who hold the shift's role (job title); cards, programs, hours and the group norm are still not checked.
- `coachcover_one_offer_at_a_time`: CoachCover before 2026-11-07: one offer at a time, plan B only when the queue is empty (the 'before' column of the before/after table).

## Assumptions

Every value is an assumption of this replay, not a measurement of a real school.

| Parameter | Value | Meaning |
|---|---|---|
| `p_awake` | 0.5 | Share of staff already awake when the first message comes (the rest wake between wake_from and wake_to). |
| `wake_from` | 05:15 | Earliest wake-up time of a person who was asleep (venue time). |
| `wake_to` | 07:30 | Latest wake-up time of a person who was asleep (venue time). |
| `reply_lag_min` | 1.0 | Minutes from seeing a message to answering it, lower bound (uniform). |
| `reply_lag_max` | 6.0 | Minutes from seeing a message to answering it, upper bound (uniform). |
| `p_willing_role` | 0.35 | Chance that a free person who holds the shift's role wants the shift that morning. |
| `p_willing_other` | 0.05 | Chance that a free person without the shift's role still answers 'I can come' in the chat. |
| `manager_notice_minutes` | 10.0 | Minutes from a call-out (or a plan B card) to the manager seeing it at dawn. |
| `chat_post_minutes` | 2.0 | Minutes the manager spends writing one post in the staff chat. |
| `chat_wait_minutes` | 10.0 | Minutes the manager waits for the chat before starting to phone people. |
| `call_minutes` | 3.0 | Minutes per phone call: dialing, ringing, voicemail or a short talk. |
| `p_pickup_awake` | 0.8 | Chance that an awake person picks up the manager's call. |
| `p_pickup_asleep` | 0.35 | Chance that a sleeping person is woken by the call and picks up. |
| `confirm_minutes` | 1.0 | Minutes the manager spends confirming the person or deciding the fallback. |
| `intake_minutes` | 1.0 | Minutes from the call-out to the first offer in CoachCover (voice to text to case). |
| `decide_minutes` | 1.0 | Minutes the manager spends on a plan B card after seeing it. |
| `callouts_weights` | [0.5, 0.3, 0.2] | Chance of 1, 2 or 3 call-outs on one morning. |
| `callout_from` | 04:30 | Earliest call-out time (venue time). |
| `callout_to` | 05:55 | Latest call-out time (venue time); every call-out comes before 06:00. |
| `callout_lead_minutes` | 15 | A call-out comes at least this many minutes before the person's first shift. |
| `morning_until` | 09:00 | Shifts of the absent person that start before this time need cover. |
| `card_changes` | [3, 7] | How many people get one card moved to expire around the morning (min, max). |
| `card_offset_days` | [-21, 20] | Range of the new expiry date, in days from the morning (min, max). |
| `p_card_expired` | 0.5 | Share of moved cards that expire before the morning. |
| `p_card_on_the_day` | 0.15 | Share of moved cards that expire on the morning itself (allowed, with a warning). |
| `p_card_unconfirmed` | 0.3 | Chance that one card's two readings disagree and nobody has confirmed it yet. |
| `hours_changes` | [2, 5] | How many people get extra hours this week (min, max). |
| `hours_target` | [34.0, 40.0] | Their weekly total after the extra hours, before any cover (min, max); the limit is the policy's. |
| `rota_swaps` | [0, 3] | How many of the morning's lessons are handed to another cleared instructor (min, max). |
| `p_renewal_overdue` | 0.4 | Chance that one instructor's program renewal is overdue that week. |

Policy of the preset: offer timer 5 min, weekly limit 40 h, autonomy {'assign_cover': 'auto', 'plan_b': 'propose', 'merge_groups': 'propose', 'overtime': 'propose', 'night_cover': 'propose'}.

## Limits of this replay

- Synthetic data: one fictional school, one week, changed per morning. Real numbers need a real school's schedule.
- The cases of one morning are worked one after another in call-out order; a person booked for an earlier case is busy for a later one that overlaps.
- People answer the chat, a call and an offer with the same delay and the same wish; the chat reaches everyone at once, CoachCover asks a wave of three at a time.
- The baseline manager never checks anything. A careful manager would check some things; the replay counts what nobody checks.
- A shift counts as closed when a person is booked or the fallback is decided; the time runs from the call-out.
- In plan B a reserve person is booked on the manager's approval without being asked, as the core does; the baseline manager never calls a reserve person.
- "Closed without a person" is 0 for the baseline by construction: the manager books every shift by hand.
- Plan B by the deadline: the manager acts on it after noticing the card; the offers of the open wave keep running until then.
