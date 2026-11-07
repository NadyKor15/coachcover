"""Database fixes from the adversarial review of stages A+B (items 1, 2, 3, 4, 5, 8, 9, 10) and the DSN mask."""

from datetime import timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from coachcover.model import Shift
from coachcover.repo import load_context, load_policy, load_shift, load_staff
from coachcover.rules import Code, assess, eligible, rank
from coachcover.seed import DECK_NOW as NOW  # Marina's 06:00 deck shift and the lesson next door: the deck story
from coachcover.seed import POLICY_ID
from tests.conftest import masked_dsn

pytestmark = pytest.mark.db

MARINA_0600 = "L-1109-G002"
NEIGHBOUR_0600 = "L-1109-G001"


@pytest.fixture
def school(deck_school):
    """These checks read Marina's 06:00 deck shift: the deck story of the training school."""
    return deck_school


def _rejected(conn, statement, params=(), error=psycopg.Error):
    with conn.transaction(force_rollback=True):
        with pytest.raises(error):
            with conn.transaction():
                conn.execute(statement, params)


# 1. the absent person comes back from the database as the shift's assignee
def test_review_1_absent_person_is_not_a_candidate_on_real_data(school):
    shift = load_shift(school, MARINA_0600)
    assert shift.assignee == "S05"
    policy = load_policy(school, POLICY_ID)
    ranked = rank(assess(s, shift, NOW, policy, load_context(school, POLICY_ID, shift)) for s in load_staff(school, POLICY_ID))
    marina = next(r for r in ranked if r.candidate.staff_id == "S05")
    assert not marina.candidate.decision.ok
    assert Code.IS_THE_ABSENT_PERSON in marina.candidate.decision.codes
    assert ranked[0].candidate.staff_id != "S05"


# 2. a card is trusted only when both readings carry fields, agree, and match the stored dates
CERT = "INSERT INTO staff_cert (staff_id, cert_type_id, issued_on, expires_on, reader_a, reader_b) VALUES (%s, (SELECT id FROM cert_type WHERE code = 'cpr_aed'), %s, %s, %s, %s) RETURNING needs_review"


def _needs_review(conn, reader_a, reader_b, issued="2025-01-01", expires="2027-01-01"):
    with conn.transaction(force_rollback=True):
        return conn.execute(CERT, ("S01", issued, expires, Jsonb(reader_a), Jsonb(reader_b))).fetchone()[0]


def test_review_2_card_trust_cannot_be_bypassed(school):
    agree = {"type": "cpr_aed", "issued_on": "2025-01-01", "expires_on": "2027-01-01"}
    a, b = {"model": "muse-glimmer-30b", "fields": agree}, {"model": "nemotron-3-nano-omni-30b-a3b-reasoning", "fields": agree}
    assert _needs_review(school, a, b) is False  # control: an honest pair is trusted
    _rejected(school, CERT, ("S01", "2025-01-01", "2027-01-01", Jsonb({}), Jsonb({})), psycopg.errors.CheckViolation)
    assert _needs_review(school, {"model": "x"}, {"model": "y"}) is True  # readers named, nothing read
    assert _needs_review(school, {**a, "fields": {**agree, "type": "wsi"}}, {**b, "fields": {**agree, "type": "wsi"}}) is True
    expired = {**agree, "expires_on": "2026-11-01"}
    assert _needs_review(school, {**a, "fields": expired}, {**b, "fields": expired}, expires="2028-11-01") is True
    _rejected(school, CERT, ("S01", "2025-01-01", "2027-01-01", Jsonb(a), Jsonb({**a})), psycopg.errors.CheckViolation)
    assert school.execute("SELECT count(*) FROM staff_cert WHERE needs_review").fetchone()[0] == 1  # seed unchanged


# 3. a successful model call must name the model that answered
def test_review_3_ok_call_without_returned_model_is_rejected(school):
    _rejected(
        school,
        "INSERT INTO model_call (at, purpose, model_requested, model_returned, source, ok)"
        " VALUES (now(), 'extract', 'muse-glimmer-30b', NULL, 'live', true)",
        error=psycopg.errors.CheckViolation,
    )


# 4. plan B merge on real data: the neighbour takes both groups on a merged shift
def test_review_4_merge_on_real_data(school):
    policy = load_policy(school, POLICY_ID)
    staff = {s.id: s for s in load_staff(school, POLICY_ID)}
    marina, neighbour_shift = load_shift(school, MARINA_0600), load_shift(school, NEIGHBOUR_0600)
    merged = Shift(
        "PLANB-1109-0600", marina.venue, marina.role, marina.start, marina.end,
        marina.groups + neighbour_shift.groups, 1, marina.zone,
        merged_shift_ids=(NEIGHBOUR_0600,),
    )
    decision = eligible(staff[neighbour_shift.assignee], merged, NOW, policy, load_context(school, POLICY_ID, merged))
    # the merge is judged on the norm (14 children, 1:4 for the young ones), not refused as a clash with his own shift
    assert Code.OVERLAPS_COMMITMENT not in decision.codes
    assert decision.codes == [Code.RATIO_EXCEEDED] and decision.reasons[0].params["capacity"] == 4


# 5. the case file is append-only: no update, no delete, no truncate, no cascade
def _case_with_event(conn):
    case_id = conn.execute(
        "INSERT INTO cover_case (shift_id, absent_staff_id, source, opened_at) VALUES (%s, 'S05', 'callout', %s) RETURNING id",
        (MARINA_0600, NOW),
    ).fetchone()[0]
    conn.execute("INSERT INTO case_event (case_id, seq, at, actor, kind) VALUES (%s, 1, %s, 'agent', 'callout_received')", (case_id, NOW))
    return case_id


@pytest.mark.parametrize("statement", ["DELETE FROM case_event", "TRUNCATE case_event", "DELETE FROM cover_case"])
def test_review_5_case_file_is_append_only(school, statement):
    with school.transaction(force_rollback=True):
        _case_with_event(school)
        with pytest.raises(psycopg.Error):
            with school.transaction():
                school.execute(statement)


# 8. the offer timer and "first yes" are enforced by the schema
OFFER = (
    "INSERT INTO offer (case_id, shift_id, staff_id, queue_pos, place, sent_at, expires_at, status, responded_at)"
    " VALUES (%s, %s, %s, 1, '{}', %s, %s, 'accepted', %s)"
)


def test_review_8_answers_only_inside_the_offer_window(school):
    for answered in (NOW + timedelta(hours=3), NOW - timedelta(hours=1)):
        with school.transaction(force_rollback=True):
            case_id = _case_with_event(school)
            with pytest.raises(psycopg.errors.CheckViolation):
                with school.transaction():
                    school.execute(OFFER, (case_id, MARINA_0600, "S17", NOW, NOW + timedelta(minutes=5), answered))


def test_review_8_accepted_offer_and_approved_plan_b_exclude_each_other(school):
    plan = (
        "INSERT INTO plan_b (case_id, snapshot, snapshot_hash, status, created_at, decided_by, decided_at, chosen_option)"
        " VALUES (%s, '{}', repeat('a', 64), 'approved', %s, 'manager:x', %s, 0)"
    )
    with school.transaction(force_rollback=True):
        case_id = _case_with_event(school)
        school.execute(OFFER, (case_id, MARINA_0600, "S17", NOW, NOW + timedelta(minutes=5), NOW + timedelta(minutes=1)))
        with pytest.raises(psycopg.Error, match="already"):
            with school.transaction():
                school.execute(plan, (case_id, NOW, NOW))
    with school.transaction(force_rollback=True):
        case_id = _case_with_event(school)
        school.execute(plan, (case_id, NOW, NOW))
        with pytest.raises(psycopg.Error, match="already"):
            with school.transaction():
                school.execute(OFFER, (case_id, MARINA_0600, "S17", NOW, NOW + timedelta(minutes=5), NOW + timedelta(minutes=1)))


def test_review_8_one_accepted_offer_per_shift_across_cases(school):
    with school.transaction(force_rollback=True):
        first = _case_with_event(school)
        school.execute(OFFER, (first, MARINA_0600, "S17", NOW, NOW + timedelta(minutes=5), NOW + timedelta(minutes=1)))
        school.execute("UPDATE cover_case SET status = 'cancelled', closed_at = %s WHERE id = %s", (NOW + timedelta(minutes=2), first))
        second = school.execute(
            "INSERT INTO cover_case (shift_id, source, opened_at) VALUES (%s, 'manager', %s) RETURNING id", (MARINA_0600, NOW)
        ).fetchone()[0]
        with pytest.raises(psycopg.errors.UniqueViolation):
            with school.transaction():
                school.execute(OFFER, (second, MARINA_0600, "S08", NOW, NOW + timedelta(minutes=5), NOW + timedelta(minutes=1)))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):  # the offer's shift must be the case's shift
            with school.transaction():
                school.execute(OFFER, (second, NEIGHBOUR_0600, "S08", NOW, NOW + timedelta(minutes=5), NOW + timedelta(minutes=1)))


# 9. autonomy values must be strings from the list
@pytest.mark.parametrize("bad", ["1", "null", '["auto"]', "true", '{"x": 1}'])
def test_review_9_autonomy_rejects_non_strings(school, bad):
    _rejected(school, "UPDATE policy SET autonomy = autonomy || %s::jsonb", (f'{{"overtime": {bad}}}',), psycopg.errors.CheckViolation)


# 10. the venue time zone must be a named zone (POSIX strings like UTC+3 mean the opposite offset)
@pytest.mark.parametrize("tz", ["UTC+3", "EST+5", "<+03>-3", "Mars/Base"])
def test_review_10_venue_time_zone_must_be_named(school, tz):
    _rejected(school, "INSERT INTO venue (id, policy_id, name, tz) VALUES ('VX', 'demo-school', 'X', %s)", (tz,))


def test_review_10_named_zones_still_pass(school):
    with school.transaction(force_rollback=True):
        school.execute("INSERT INTO venue (id, policy_id, name, tz) VALUES ('VX', 'demo-school', 'X', 'Europe/Madrid')")


def test_test_dsn_password_is_masked():
    shown = masked_dsn("postgresql://app:s3cr3t-value@127.0.0.1:5432/db")
    assert "s3cr3t-value" not in shown and "127.0.0.1" in shown
    plain = masked_dsn("postgresql://postgres@127.0.0.1:55432/postgres")
    assert "dbname=postgres" in plain and "***" not in plain
