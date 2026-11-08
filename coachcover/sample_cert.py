"""The judge's sample certificate check: both card readers read one bundled synthetic card.

The cards in coachcover/samples/cards are synthetic (fictional people and issuers), the same images the
2026-11-06 model check read; replay mode answers from those recorded readings (tests/fixtures/models/card-*).
Each card belongs to a fictional applicant who is not on the roster: an inactive staff row with no roles, so
the certificate row the check writes can never put that person on a shift, and the night check skips them.
Every judge scene drops and rebuilds the public schema first (coachcover.demo.reset_school), which removes the
applicants, their certificate rows and the ledger rows of the check.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg

from coachcover import money as money_model
from coachcover.models import MODEL_LABELS, SECOND_READER_MODEL, TEXT_MODEL, ModelClient
from coachcover.pipeline import Pipeline, _card_date, plain

CARDS_DIR = Path(__file__).resolve().parent / "samples" / "cards"
READER_NAMES = {TEXT_MODEL: "Muse Glimmer", SECOND_READER_MODEL: "Nemotron Omni"}
APPLICANT_PREFIX = "SAMPLE-"


@dataclass(frozen=True)
class SampleCard:
    id: str
    file: str
    mime: str
    cert_type: str
    holder: str  # the name printed on the card (the generator's truth file)
    label: str

    @property
    def image_url(self) -> str:
        return f"/media/sample-cards/{self.file}"

    @property
    def staff_id(self) -> str:
        return APPLICANT_PREFIX + self.id.upper()


# the cards and their order (a clear card first, then one whose dates the readers read differently) are the
# training school's data, in samples.json next to the images
SAMPLES: tuple[SampleCard, ...] = tuple(
    SampleCard(**{k: s[k] for k in ("id", "file", "mime", "cert_type", "holder", "label")})
    for s in json.loads((CARDS_DIR / "samples.json").read_text(encoding="utf-8"))["samples"]
)
BY_ID = {s.id: s for s in SAMPLES}
BY_FILE = {s.file: s for s in SAMPLES}

FLAG_WORDS = {
    "dates_disagree": "the two readers read different dates",
    "name_mismatch": "the name on the card is not the applicant's",
    "unreadable_dates": "the dates could not be read",
    "unknown_type": "the card type is not one the school uses",
}


def pick(conn: psycopg.Connection, sample_id: str | None) -> SampleCard:
    """The named sample, or the next one in turn (counted from the checks since the last reset)."""
    if sample_id is not None:
        if sample_id not in BY_ID:
            raise ValueError(f"sample must be one of {tuple(BY_ID)}")
        return BY_ID[sample_id]
    done = conn.execute(
        "SELECT count(DISTINCT ref->>'sample_check') FROM model_call WHERE ref ? 'sample_check'"
    ).fetchone()[0]
    return SAMPLES[done % len(SAMPLES)]


def _applicant(conn: psycopg.Connection, policy_id: str, sample: SampleCard) -> str:
    conn.execute(
        "INSERT INTO staff (id, policy_id, full_name, home_venue_id, language, active) VALUES (%s, %s, %s, NULL, 'en', false)"
        " ON CONFLICT (id) DO NOTHING",
        (sample.staff_id, policy_id, sample.holder),
    )
    return sample.staff_id


def _ledger_rows(conn: psycopg.Connection, check_id: str, tz: ZoneInfo) -> list[dict]:
    prices = money_model.load_prices()
    rows = []
    for at, requested, returned, ms, tin, tout, cost, source, recording, synthetic, ok, error in conn.execute(
        "SELECT at, model_requested, model_returned, duration_ms, input_tokens, output_tokens, cost_usd, source,"
        " ref->>'recording', coalesce((ref->>'synthetic')::boolean, false), ok, error FROM model_call"
        " WHERE ref->>'sample_check' = %s ORDER BY id",
        (check_id,),
    ).fetchall():
        model = returned or requested
        usd, _ = money_model.call_price({"model_returned": returned, "model_requested": requested, "input_tokens": tin,
                                         "output_tokens": tout, "cost_usd": cost}, prices)
        rows.append({
            "model": model,
            "model_label": MODEL_LABELS.get(model, model),
            "reader": READER_NAMES.get(requested, requested),
            "at": at,
            "time_label": f"{at.astimezone(tz):%H:%M:%S}",
            "ms": ms,
            "seconds_label": f"{ms / 1000:.1f} s" if ms is not None else None,
            "input_tokens": tin,
            "output_tokens": tout,
            "cost_usd": usd,
            "cost_label": money_model.cents_label(usd * 100) if usd is not None else None,
            "source": source,
            "source_label": ("live call on Vultr" if source == "live" else
                             f"recorded answer ({recording})" + (", prepared" if synthetic else "") if recording
                             else "no recorded answer"),
            "synthetic": synthetic,
            "ok": ok,
            "error": error,
        })
    return rows


def check(conn: psycopg.Connection, models: ModelClient, policy_id: str, now: datetime, tz: ZoneInfo,
          sample_id: str | None = None) -> dict:
    """Read one sample card with both readers through Pipeline.read_certificate; the answer for the judge's panel."""
    sample = pick(conn, sample_id)
    staff_id = _applicant(conn, policy_id, sample)
    check_id = uuid.uuid4().hex
    image = (CARDS_DIR / sample.file).read_bytes()
    result = Pipeline(conn, models, policy_id).read_certificate(
        staff_id, image, sample.mime, now, cert_type=sample.cert_type, ref={"sample_check": check_id, "sample": sample.id})
    ledger = _ledger_rows(conn, check_id, tz)
    by_model = {row["model"]: row for row in ledger}
    readings = []
    for model in (TEXT_MODEL, SECOND_READER_MODEL):
        got = result["readings"].get(model)
        row = by_model.get(model, {})
        readings.append({
            "model": model,
            "reader": READER_NAMES[model],
            "ok": got is not None,
            "name": (got or {}).get("name"),
            "card_number": (got or {}).get("id"),
            "issued": _card_date((got or {}).get("issued")),
            "expires": _card_date((got or {}).get("exp")),
            "issued_raw": (got or {}).get("issued"),
            "expires_raw": (got or {}).get("exp"),
            "ms": row.get("ms"),
            "seconds_label": row.get("seconds_label"),
            "cost_usd": row.get("cost_usd"),
            "cost_label": row.get("cost_label"),
            "source": row.get("source"),
            "error": row.get("error"),
        })
    a, b = readings
    dates_agree = (a["issued"], a["expires"]) == (b["issued"], b["expires"]) if a["ok"] and b["ok"] else None
    flags, notes = result["flags"], result["notes"]
    failed = [f.split(":", 1)[1] for f in flags if f.startswith("reader_failed:")]
    card = result["cards"][0] if result["cards"] else None
    accepted = not result["review"]
    if failed:
        verdict = "Reader failed: sent to a person"
    elif "dates_disagree" in flags:
        verdict = "Dates disagree: sent to a person"
    elif accepted:
        verdict = "Dates agree: accepted"
    else:
        why = [FLAG_WORDS[f] for f in flags if f in FLAG_WORDS] or ["the card needs a person's check"]
        verdict = "Sent to a person: " + "; ".join(why)
    today = now.astimezone(tz).date()
    expires = date.fromisoformat(card["expires_on"]) if card and card["expires_on"] else None
    priced = [r["cost_usd"] for r in ledger if r["cost_usd"] is not None]
    total = round(sum(priced), 10)
    return plain({
        "check_id": check_id,
        "sample": {"id": sample.id, "label": sample.label, "image_url": sample.image_url, "cert_type": sample.cert_type,
                   "synthetic": True},
        "applicant": {"staff_id": staff_id, "name": sample.holder, "on_roster": False},
        "readings": readings,
        "dates_agree": dates_agree,
        "outcome": "accepted" if accepted else "sent_to_person",
        "verdict": verdict,
        "flags": flags,
        "notes": notes,
        "card": None if card is None else {**card, "expired": expires is not None and expires < today},
        "checked_on": today,
        "mode": models.mode,
        "ledger": ledger,
        "total_cost_usd": total,
        "total_cost_label": money_model.cents_label(total * 100) if priced else None,
        "unpriced_calls": len(ledger) - len(priced),
    })
