# Two card readers on 30 hard photos (check of 2026-11-06)

Why CoachCover reads every certificate card twice, with two different models, and trusts the dates only when
both readings agree.

## What was measured

- 30 synthetic certificate card photos: fictional people and issuers, made by a generator for this check.
  The photos are made hard on purpose: rotation up to 16°, glare, a finger over a corner, blur (1.0–2.8),
  JPEG quality 25–55, 640–1000 px wide, four date formats, and two cards with no valid-through date at all.
- Reader A: `muse-glimmer-30b` (Muse Glimmer). Reader B: `nemotron-3-nano-omni-30b-a3b-reasoning` (Nemotron Omni).
  Both on Vultr Serverless Inference, the same prompt and answer schema the product uses.
- HTTP errors: 0. Answers from a model other than the one asked for: 0.

## Results

| Fields | Reader A alone wrong | Reader B alone wrong | Two readers: agreed, both wrong | Two readers: sent to a person |
|---|---|---|---|---|
| Dates (issued, valid through) — they decide who may work; 60 fields | 7 | 3 | **0** | 8 cards of 30 |
| All 4 fields (name, card number, dates); 120 fields | 20 | 20 | **0** | 18 cards of 30 (mostly a small card-number difference) |

- With one reader, 7 of the 30 cards would have silently got a wrong date: a false clearance or a false refusal.
  With two readers, no wrong date was accepted; the price is a person's check of 8 of 30 cards.
- Cards with no valid-through date: both readers correctly returned `null` (2 of 2).
- Median time per card: reader A 1.4 s, reader B 7.7 s.

## What the product does with it

- A card is trusted automatically only when both readers agree on the dates (the database enforces it too).
- Readers that differ only on the card number leave a "check ID" note; that is not a block.
- A reader that fails, a date disagreement, a name that is not the person's or an unknown card type sends the
  card to a person.

## See it

- The recorded readings of this check: `tests/fixtures/models/card-hard-*.json`; the photos:
  `tests/fixtures/cards/hard/`. `tests/test_filter_readers.py` replays all 30 and asserts 0 wrong dates
  accepted and 8 cards sent to a person.
- "Check a sample certificate" (`POST /api/judge/sample-certificate`) lets a judge make both readers read one
  of five bundled synthetic cards and see both readings, the verdict and the model ledger rows. One of the five
  (`h22-photo`, glare over the date) is a card from this check whose valid-through date the readers read
  differently.

These are synthetic photos and a stress test, not cards photographed in the field.
