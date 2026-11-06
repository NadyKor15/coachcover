"""The cover pipeline end to end on replay, on a fresh training school per test."""

from datetime import timedelta
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

from coachcover.models import DbLedger, ModelClient
from coachcover.pipeline import Conflict, NotFound, Pipeline
from coachcover.seed import NOW, POLICY_ID, load_school

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).parent / "fixtures"
RU_VOICE = (FIXTURES / "audio" / "ru-callout.ogg").read_bytes()
LANE_0600, KIDS_0700, NEXT_DOOR_0600 = "L-1109-G001", "L-1109-G004", "L-1109-G002"
TRAPS = {"S07", "S10", "S13", "S14"}  # expired cards, disputed card, overdue program
RESERVE = {"S06", "S12", "S23"}


def minutes(n: int):
    return NOW + timedelta(minutes=n)


@pytest.fixture
def school(new_database):
    conn = new_database()
    load_school(conn)
    return conn


def pipeline(conn, **autonomy) -> Pipeline:
    if autonomy:
        conn.execute("UPDATE policy SET autonomy = autonomy || %s WHERE id = %s", (Jsonb(autonomy), POLICY_ID))
    return Pipeline(conn, ModelClient(DbLedger(conn), mode="replay"), POLICY_ID)


def pending(conn, case_id):
    row = conn.execute("SELECT id, staff_id FROM offer WHERE case_id = %s AND status = 'pending'", (case_id,)).fetchone()
    return row if row else (None, None)


def events(conn, case_id):
    return [k for (k,) in conn.execute("SELECT kind FROM case_event WHERE case_id = %s ORDER BY seq", (case_id,))]


def voice_callout(p):
    result = p.callout("S05", now=NOW, audio=RU_VOICE, filename="ru-callout.ogg")
    return {c["shift_id"]: c["case_id"] for c in result["cases"]}, result


def decline_all(conn, p, case_id, start_minute=1):
    offered, t = [], start_minute
    while True:
        offer_id, staff_id = pending(conn, case_id)
        if offer_id is None:
            return offered, t
        offered.append(staff_id)
        p.respond(offer_id, False, minutes(t))
        t += 1


# scenario 1: Russian voice note -> Marina's 06:00 -> filter -> queue -> second person says yes -> covered
def test_voice_callout_to_covered_shift_with_case_file(school):
    p = pipeline(school)
    cases, result = voice_callout(p)
    assert result["text"].startswith("Привет, это Марина")
    assert result["understood"]["start_times"] == ["06:00", "07:00"]
    assert list(cases) == [LANE_0600, KIDS_0700]
    case = cases[LANE_0600]
    first_offer, first = pending(school, case)
    assert first not in TRAPS | RESERVE | {"S05"}
    p.respond(first_offer, False, minutes(2))  # 05:14 no
    second_offer, second = pending(school, case)
    assert second != first
    state = p.respond(second_offer, True, minutes(6))  # 05:18 yes, inside the 5-minute window from 05:14
    assert state == {"offer_id": second_offer, "case_id": case, "staff_id": second, "status": "accepted", "case_status": "covered"}
    assert school.execute("SELECT staff_id, status FROM shift WHERE id = %s", (LANE_0600,)).fetchone() == (second, "scheduled")
    assert events(school, case) == [
        "callout_received", "voice_transcribed", "callout_understood", "shift_opened", "candidates_ranked",
        "offer_sent", "offer_declined", "offer_sent", "shift_covered",
    ]
    covered = school.execute("SELECT data FROM case_event WHERE case_id = %s AND kind = 'shift_covered'", (case,)).fetchone()[0]
    assert covered["minutes_to_cover"] == 6.0
    calls = school.execute(
        "SELECT purpose, model_returned, ok, cost_usd > 0 FROM model_call WHERE ref->>'callout_id' = %s ORDER BY id",
        (result["callout_id"],),
    ).fetchall()
    assert calls == [("transcribe", "whisper-large-v3-turbo", True, True), ("extract_callout", "muse-glimmer-30b", True, True)]
    staff_msgs = school.execute(
        "SELECT staff_id, count(*) FROM message WHERE case_id = %s AND audience = 'staff' GROUP BY staff_id ORDER BY 1", (case,)
    ).fetchall()
    assert dict(staff_msgs) == {first: 1, second: 2}  # two offers, one confirmation
    assert school.execute("SELECT count(*) FROM message WHERE channel <> 'simulated'").fetchone()[0] == 0


# scenario 2: nobody takes it -> plan B -> approval by hash -> 8 families told
def test_nobody_accepts_then_plan_b_merge_approved_by_hash(school):
    p = pipeline(school)
    cases, _ = voice_callout(p)
    case = cases[LANE_0600]
    eligible_now = {q["staff_id"] for q in p.candidates(LANE_0600, NOW)["queue"] if q["eligible"]}
    offered, _ = decline_all(school, p, case)
    assert set(offered) == eligible_now and len(offered) == len(set(offered))
    assert not set(offered) & (TRAPS | RESERVE | {"S05"})  # nobody blocked ever gets an offer
    plan_id, snapshot, digest = school.execute(
        "SELECT id, snapshot, snapshot_hash FROM plan_b WHERE case_id = %s AND status = 'proposed'", (case,)
    ).fetchone()
    kinds = [o["kind"] for o in snapshot["options"]]
    assert kinds[0] == "merge" and kinds[-2:] == ["reschedule", "close"]
    merge = snapshot["options"][0]
    assert (merge["into_shift_id"], merge["participants"], merge["capacity"], merge["mode"]) == (NEXT_DOOR_0600, 8, 8, "propose")
    with pytest.raises(Conflict, match="changed"):
        p.approve_plan_b(plan_id, "0" * 64, minutes(30), by="manager:test")
    outcome = p.approve_plan_b(plan_id, digest, minutes(30), by="manager:test")
    assert outcome == {"plan_id": plan_id, "option": "merge", "families": 8}
    told = school.execute(
        "SELECT DISTINCT m.group_id, g.size FROM message m JOIN lesson_group g ON g.id = m.group_id"
        " WHERE m.case_id = %s AND m.audience = 'family'",
        (case,),
    ).fetchall()
    assert {g for g, _ in told} == {"G001", "G002"} and sum(size for _, size in told) == 8
    assert school.execute("SELECT status FROM shift WHERE id = %s", (LANE_0600,)).fetchone() == ("cancelled",)
    assert [g for (g,) in school.execute("SELECT group_id FROM shift_group WHERE shift_id = %s ORDER BY 1", (NEXT_DOOR_0600,))] == ["G001", "G002"]
    assert school.execute("SELECT status FROM cover_case WHERE id = %s", (case,)).fetchone() == ("plan_b",)
    with pytest.raises(Conflict):
        p.approve_plan_b(plan_id, digest, minutes(31), by="manager:test")  # already approved


def test_a_changed_situation_turns_approval_into_a_conflict_with_a_new_plan(school):
    p = pipeline(school)
    cases, _ = voice_callout(p)
    case = cases[LANE_0600]
    decline_all(school, p, case)
    plan_id, digest = school.execute("SELECT id, snapshot_hash FROM plan_b WHERE case_id = %s AND status = 'proposed'", (case,)).fetchone()
    teacher = school.execute("SELECT staff_id FROM shift WHERE id = %s", (NEXT_DOOR_0600,)).fetchone()[0]
    p.callout(teacher, now=minutes(25), text="Sorry, I can't come in this morning.")  # the merge partner calls out too
    with pytest.raises(Conflict, match="new plan") as err:
        p.approve_plan_b(plan_id, digest, minutes(30), by="manager:test")
    new_id = err.value.detail["plan_id"]
    assert new_id != plan_id
    assert school.execute("SELECT status FROM plan_b WHERE id = %s", (plan_id,)).fetchone() == ("superseded",)
    new_snapshot = school.execute("SELECT snapshot FROM plan_b WHERE id = %s", (new_id,)).fetchone()[0]
    assert all(o.get("into_shift_id") != NEXT_DOOR_0600 for o in new_snapshot["options"])


# scenario 3: closure -> lessons cancelled, families told in their languages, credits
def test_closure_cancels_lessons_and_gives_credits(school):
    p = pipeline(school)
    cases, _ = voice_callout(p)
    start, end = NOW.replace(hour=6, minute=0), NOW.replace(hour=8, minute=0)
    result = p.closure("V1", "V1-TEACH", "weather", start, end, now=minutes(10), created_by="manager:test")
    assert result["cancelled_shifts"] == [NEXT_DOOR_0600, KIDS_0700]
    assert result["remedy"] == "credit" and result["families"] == 4 + 3
    assert set(result["languages"]) <= {"en", "es", "ru"} and result["messages"] >= 2
    remedies = school.execute("SELECT group_id, kind, families FROM remedy WHERE closure_id = %s ORDER BY 1", (result["closure_id"],)).fetchall()
    assert remedies == [("G002", "credit", 4), ("G004", "credit", 3)]
    assert school.execute("SELECT status FROM cover_case WHERE id = %s", (cases[KIDS_0700],)).fetchone() == ("cancelled",)
    assert school.execute("SELECT count(*) FROM offer WHERE case_id = %s AND status = 'pending'", (cases[KIDS_0700],)).fetchone() == (0,)
    texts = school.execute("SELECT language, body FROM message WHERE closure_id = %s ORDER BY id", (result["closure_id"],)).fetchall()
    assert all("{group}" not in body and "{time}" not in body for _, body in texts)
    with pytest.raises(ValueError):
        p.closure("V1", None, "aliens", start, end, now=minutes(10), created_by="manager:test")


# certificate cards with two readers
def _add_person(conn, sid, name):
    conn.execute("INSERT INTO staff (id, policy_id, full_name, home_venue_id) VALUES (%s, %s, %s, 'V1')", (sid, POLICY_ID, name))


def test_readers_disagree_so_the_card_goes_to_a_person(school):
    p = pipeline(school)
    _add_person(school, "S98", "Jake Turner")
    result = p.read_certificate("S98", (FIXTURES / "cards" / "c5-photo.jpg").read_bytes(), "image/jpeg", NOW)
    assert "readers_disagree" in result["flags"] and result["review"] is True
    assert school.execute("SELECT needs_review FROM staff_cert WHERE staff_id = 'S98'").fetchall() == [(True,)]


def test_matching_readers_and_name_give_a_trusted_card(school):
    p = pipeline(school)
    _add_person(school, "S98", "Jake Turner")
    result = p.read_certificate("S98", (FIXTURES / "cards" / "c2-scan.png").read_bytes(), "image/png", NOW)
    assert result["flags"] == [] and result["review"] is False
    assert [(c["type"], c["expires_on"]) for c in result["cards"]] == [("wsi", "2026-03-15")]
    assert school.execute("SELECT needs_review FROM staff_cert WHERE staff_id = 'S98'").fetchall() == [(False,)]


def test_combined_card_and_wrong_person(school):
    p = pipeline(school)
    _add_person(school, "S98", "Maria Lopez")
    combined = p.read_certificate("S98", (FIXTURES / "cards" / "c1-scan.png").read_bytes(), "image/png", NOW)
    assert sorted(c["type"] for c in combined["cards"]) == ["cpr_aed", "lifeguarding"] and not combined["review"]
    other = p.read_certificate("S01", (FIXTURES / "cards" / "c1-photo.jpg").read_bytes(), "image/jpeg", NOW)
    assert "name_mismatch" in other["flags"] and all(c["review"] for c in other["cards"])


# autonomy: "propose" leaves a card, "off" does nothing, "auto" acts
def test_assign_cover_propose_waits_for_a_person(school):
    p = pipeline(school, assign_cover="propose")
    cases, _ = voice_callout(p)
    case = cases[LANE_0600]
    offer_id, staff_id = pending(school, case)
    p.respond(offer_id, True, minutes(2))
    approval_id, status = school.execute("SELECT id, status FROM approval WHERE case_id = %s", (case,)).fetchone()
    assert status == "pending"
    assert school.execute("SELECT status FROM shift WHERE id = %s", (LANE_0600,)).fetchone() == ("open",)
    p.decide_approval(approval_id, True, minutes(4), by="manager:test")
    assert school.execute("SELECT staff_id, status FROM shift WHERE id = %s", (LANE_0600,)).fetchone() == (staff_id, "scheduled")


def test_assign_cover_off_sends_no_offers(school):
    p = pipeline(school, assign_cover="off")
    cases, _ = voice_callout(p)
    assert school.execute("SELECT count(*) FROM offer").fetchone() == (0,)
    assert "offers_off" in events(school, cases[LANE_0600])


def test_plan_b_and_merge_on_auto_apply_without_a_person(school):
    p = pipeline(school, plan_b="auto", merge_groups="auto")
    cases, _ = voice_callout(p)
    decline_all(school, p, cases[LANE_0600])
    assert school.execute("SELECT status, decided_by FROM plan_b WHERE case_id = %s", (cases[LANE_0600],)).fetchone() == ("approved", "agent")


def test_overtime_is_only_proposed_with_its_price_and_then_booked(school):
    p = pipeline(school)
    block = "G-1109-V1-0530"
    guard = school.execute("SELECT staff_id FROM shift WHERE id = %s", (block,)).fetchone()[0]
    result = p.callout(guard, now=NOW, text="I am sick, not coming in today.")  # no recording: falls back to today's shifts
    case = next(c["case_id"] for c in result["cases"] if c["shift_id"] == block)
    understood = school.execute("SELECT data FROM case_event WHERE case_id = %s AND kind = 'callout_understood'", (case,)).fetchone()[0]
    assert understood["by"] == "fallback" and "no recorded answer" in understood["error"]
    decline_all(school, p, case)
    plan_id, snapshot, digest = school.execute("SELECT id, snapshot, snapshot_hash FROM plan_b WHERE case_id = %s AND status = 'proposed'", (case,)).fetchone()
    overtime = next((i, o) for i, o in enumerate(snapshot["options"]) if o["kind"] == "overtime")
    index, option = overtime
    assert option["staff_id"] == "S29" and option["mode"] == "propose"
    assert (option["hours_over"], option["cost_usd"]) == (2.5, 85.0)  # 38 h + 4.5 h - 40 h, at 34 $/h
    p.approve_plan_b(plan_id, digest, minutes(40), by="manager:test", option=index)
    offer_id, staff_id = pending(school, case)
    assert staff_id == "S29"
    state = p.respond(offer_id, True, minutes(41))
    assert state["status"] == "accepted" and state["case_status"] == "covered"


# the person is checked again at the moment of "yes"; a late answer is refused
def test_yes_rechecks_the_rules_and_moves_on(school):
    p = pipeline(school)
    cases, _ = voice_callout(p)
    case = cases[LANE_0600]
    offer_id, staff_id = pending(school, case)
    school.execute(
        "INSERT INTO shift (id, venue_id, role_id, starts_at, ends_at, staff_id)"
        " SELECT 'X-CLASH', 'V2', role_id, starts_at, ends_at, %s FROM shift WHERE id = %s",
        (staff_id, LANE_0600),
    )
    state = p.respond(offer_id, True, minutes(2))
    assert state["status"] == "withdrawn"
    assert "accept_rejected" in events(school, case)
    nxt, nxt_staff = pending(school, case)
    assert nxt is not None and nxt_staff != staff_id


def test_a_late_answer_is_refused_and_the_queue_moves_on(school):
    p = pipeline(school)
    cases, _ = voice_callout(p)
    case = cases[LANE_0600]
    offer_id, staff_id = pending(school, case)
    with pytest.raises(Conflict, match="expired"):
        p.respond(offer_id, True, minutes(6))  # the timer is 5 minutes
    nxt, nxt_staff = pending(school, case)
    assert nxt is not None and nxt_staff != staff_id
    assert "offer_expired" in events(school, case)


def test_callout_validation(school):
    p = pipeline(school)
    with pytest.raises(ValueError):
        p.callout("S05", now=NOW)
    with pytest.raises(ValueError):
        p.callout("S05", now=NOW, text="  ")
    with pytest.raises(NotFound):
        p.callout("NOBODY", now=NOW, text="hi")
