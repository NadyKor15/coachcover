"""Message filter and the two card readers, on recorded answers of model check no. 2 (2026-11-06)."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from coachcover.models import MemoryLedger, ModelClient, SAFETY_MODEL
from coachcover.models import DbLedger
from coachcover.pipeline import Pipeline
from coachcover.seed import NOW, POLICY_ID, load_school

FIXTURES = Path(__file__).parent / "fixtures"
HARD = FIXTURES / "cards" / "hard"
AT = datetime(2026, 11, 9, 10, 12, tzinfo=UTC)
POLICY = "test policy"  # the replay key is the message text; the policy text does not change the recorded answer


def _filter_recordings():
    for path in sorted((FIXTURES / "models").glob("filter-[0-9]*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        yield path.stem, raw["input"]["text"], "attack" in path.stem


def test_the_filter_flags_every_attack_and_no_ordinary_message():
    client = ModelClient(MemoryLedger(), mode="replay")
    seen = list(_filter_recordings())
    assert len(seen) == 12 and sum(attack for *_, attack in seen) == 6
    for name, text, attack in seen:
        verdict = client.screen_message(text, POLICY, at=AT).data["verdict"]
        assert verdict == ("unsafe" if attack else "safe"), name
    calls = client.ledger.calls
    assert all(c.ok and c.model_returned == SAFETY_MODEL and c.cost_usd > 0 for c in calls)


@pytest.fixture
def school(new_database):
    conn = new_database()
    load_school(conn)
    return conn


def _staff_for(conn, name: str, card: str) -> str:
    """The seed's person with the card's name, or a new person for it."""
    row = conn.execute("SELECT id FROM staff WHERE full_name = %s", (name,)).fetchone()
    if row:
        return row[0]
    conn.execute("INSERT INTO staff (id, policy_id, full_name, home_venue_id) VALUES (%s, %s, %s, 'V1')", (f"X-{card}", POLICY_ID, name))
    return f"X-{card}"


def _read(conn, card: str, cert_type: str = "lifeguarding") -> dict:
    truth = json.loads((HARD / "truth.json").read_text())[card]
    staff = _staff_for(conn, truth["name"], card)
    p = Pipeline(conn, ModelClient(DbLedger(conn), mode="replay"), POLICY_ID)
    return p.read_certificate(staff, (HARD / f"{card}.jpg").read_bytes(), "image/jpeg", NOW, cert_type=cert_type)


@pytest.mark.db
def test_readers_agree_on_dates_but_not_on_the_id_trusted_with_a_note(school):
    result = _read(school, "h01")  # a test card with an earlier fictional name; readers: NAT-4ESI-10 and HAT-4851-10
    assert result["notes"] == ["check_id"] and result["flags"] == []
    assert result["review"] is False
    assert [(c["issued_on"], c["expires_on"], c["review"]) for c in result["cards"]] == [("2025-10-26", "2027-10-26", False)]
    assert school.execute("SELECT needs_review, notes FROM staff_cert WHERE id = %s", (result["cards"][0]["id"],)).fetchone() == (False, ["check_id"])


@pytest.mark.db
def test_readers_agree_on_everything_trusted(school):
    result = _read(school, "h11")  # Sofia Reyes
    assert (result["notes"], result["flags"], result["review"]) == ([], [], False)


@pytest.mark.db
def test_readers_disagree_on_a_date_blocked_until_a_person_checks(school):
    result = _read(school, "h05")  # issued 2024-02-26 against 2026-02-26
    assert "dates_disagree" in result["flags"] and result["review"] is True
    card = result["cards"][0]
    assert school.execute("SELECT needs_review FROM staff_cert WHERE id = %s", (card["id"],)).fetchone() == (True,)
    school.execute("UPDATE staff_cert SET verified_by_human = 'manager:Natalie', verified_at = now() WHERE id = %s", (card["id"],))
    assert school.execute("SELECT needs_review FROM staff_cert WHERE id = %s", (card["id"],)).fetchone() == (False,)


@pytest.mark.db
def test_a_card_without_expiry_uses_the_type_validity(school):
    result = _read(school, "h08")  # both readers: no valid-through date on the card
    card = result["cards"][0]
    assert card["expires_on"] is None and card["review"] is False


@pytest.mark.db
def test_every_hard_card_is_either_trusted_or_sent_to_a_person_never_wrongly_trusted(school):
    truth = json.loads((HARD / "truth.json").read_text())
    wrong_and_trusted = []
    to_person = 0
    for card in sorted(truth):
        result = _read(school, card)
        trusted = not result["review"]
        to_person += not trusted
        exp = truth[card]["exp"]
        if trusted and (result["cards"][0]["issued_on"], result["cards"][0]["expires_on"]) != (truth[card]["issued"], exp):
            wrong_and_trusted.append(card)
    assert wrong_and_trusted == []  # check no. 2: two readers, 0 wrong dates accepted
    assert to_person == 8  # the 8 cards whose dates the readers disagree on
