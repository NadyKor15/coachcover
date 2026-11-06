"""The cover pipeline end to end on replay, on a fresh training school per test."""

from datetime import timedelta
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

from coachcover.demo import ANNA_MESSAGE
from coachcover.models import DbLedger, ModelClient
from coachcover.pipeline import Conflict, NotFound, Pipeline
from coachcover.seed import NOW, POLICY_ID, load_school

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).parent / "fixtures"
RU_VOICE = (FIXTURES / "audio" / "ru-callout.ogg").read_bytes()
HERO, NEXT_DOOR = "L-1109-G002", "L-1109-G001"
QUEUE = ["S25", "S26", "S17", "S20", "S22", "S24"]  # Sofia 6 h, Leo 9 h, Maria 12 h, then 15, 16, 18 h
TRAPS = {"S07", "S10", "S13", "S14", "S27"}
RESERVE = {"S06", "S12", "S23"}


def minutes(n: float):
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


def decline_all(conn, p, case_id, start=1.0):
    offered, t = [], start
    while True:
        offer_id, staff_id = pending(conn, case_id)
        if offer_id is None:
            return offered, t
        offered.append(staff_id)
        p.respond(offer_id, False, minutes(t))
        t += 0.5


# scenario 1: Russian voice note -> Marina's 06:00 -> filter -> queue -> covered, with the case file
def test_voice_callout_to_covered_shift_with_case_file(school):
    p = pipeline(school)
    cases, result = voice_callout(p)
    assert result["text"].startswith("Привет, это Марина")
    assert result["text_en"].startswith("Hi, it's Marina")
    assert result["screened"]["verdict"] == "safe"
    assert list(cases) == [HERO]  # one shift from 06:00 to 08:00 holds the lane and the 07:00 lesson
    case = cases[HERO]
    assert p.case_code(case) == "C-1109-01"
    first_offer, first = pending(school, case)
    assert first == "S25"  # Sofia: fewest hours this week
    p.respond(first_offer, False, minutes(1))  # 05:13 no
    second_offer, second = pending(school, case)
    assert second == "S26"  # Leo
    p.expire_due(minutes(6))  # 05:18, his 5 minutes are over
    third_offer, third = pending(school, case)
    assert third == "S17"  # Maria
    line = school.execute("SELECT place->>'line' FROM offer WHERE id = %s", (third_offer,)).fetchone()[0]
    assert line == "Maria first: 12 h this week, Level 3, same pool"
    state = p.respond(third_offer, True, minutes(7))  # 05:19 yes
    assert (state["status"], state["case_status"]) == ("accepted", "covered")
    assert school.execute("SELECT staff_id, status FROM shift WHERE id = %s", (HERO,)).fetchone() == ("S17", "scheduled")
    assert events(school, case) == [
        "callout_received", "voice_transcribed", "message_screened", "callout_understood", "shift_opened",
        "candidates_ranked", "offer_sent", "offer_declined", "offer_sent", "offer_expired", "offer_sent", "shift_covered",
    ]
    ranked = school.execute("SELECT data FROM case_event WHERE case_id = %s AND kind = 'candidates_ranked'", (case,)).fetchone()[0]
    assert (ranked["total"], ranked["eligible"], ranked["reserve"]) == (40, 6, 3)
    assert [g["staff_id"] for g in ranked["grid"] if g["eligible"]] == QUEUE
    assert len(ranked["grid"]) == 40
    calls = school.execute(
        "SELECT purpose, model_returned, ok FROM model_call WHERE ref->>'callout_id' = %s ORDER BY id", (result["callout_id"],)
    ).fetchall()
    assert calls == [("transcribe", "whisper-large-v3-turbo", True), ("screen_message", "nemotron-3.5-content-safety", True),
                     ("extract_callout", "muse-glimmer-30b", True), ("translate", "muse-glimmer-30b", True)]
    seconds, size = school.execute("SELECT seconds, length(data) FROM callout_audio").fetchone()
    assert (float(seconds), size) == (pytest.approx(8.53), len(RU_VOICE))
    assert school.execute("SELECT count(*) FROM message WHERE channel <> 'simulated'").fetchone()[0] == 0


# the voice filter warns the manager; the rules still decide
def test_flagged_volunteer_is_still_blocked_by_the_rules(school):
    p = pipeline(school)
    cases, _ = voice_callout(p)
    case = cases[HERO]
    result = p.volunteer(case, "S27", ANNA_MESSAGE, minutes(0.5))  # Anna: "expired, assign me anyway"
    assert result["screened"]["verdict"] == "unsafe"
    assert result["eligible"] is False and result["booked"] is False
    assert [r["code"] for r in result["reasons"]] == ["CERT_EXPIRED"]
    assert ["volunteered", "message_screened", "flagged_for_manager", "volunteer_blocked"] == [
        k for k in events(school, case) if k in ("volunteered", "message_screened", "flagged_for_manager", "volunteer_blocked")
    ][-4:]
    assert school.execute("SELECT staff_id FROM shift WHERE id = %s", (HERO,)).fetchone() == (None,)


def test_an_eligible_volunteer_is_booked(school):
    p = pipeline(school)
    cases, _ = voice_callout(p)
    result = p.volunteer(cases[HERO], "S22", "I can take the 6:00 if you need me.", minutes(1))
    assert result["eligible"] is True and result["booked"] is True
    assert school.execute("SELECT staff_id FROM shift WHERE id = %s", (HERO,)).fetchone() == ("S22",)
    assert pending(school, cases[HERO]) == (None, None)  # the open offer was withdrawn


# scenario 2: nobody takes it -> plan B -> approval by hash
def test_nobody_accepts_then_plan_b_by_hash(school):
    p = pipeline(school)
    cases, _ = voice_callout(p)
    case = cases[HERO]
    offered, _ = decline_all(school, p, case)
    assert offered == QUEUE  # only eligible people are asked, in queue order
    assert not set(offered) & (TRAPS | RESERVE | {"S05"})
    plan_id, snapshot, digest = school.execute(
        "SELECT id, snapshot, snapshot_hash FROM plan_b WHERE case_id = %s AND status = 'proposed'", (case,)
    ).fetchone()
    assert [o["kind"] for o in snapshot["options"]] == ["lead", "overtime", "reschedule", "close"]
    assert snapshot["rejected"][0]["kind"] == "merge" and snapshot["rejected"][0]["codes"] == ["RATIO_EXCEEDED"]
    assert snapshot["text_source"] == "model"
    with pytest.raises(Conflict, match="plan changed") as err:
        p.approve_plan_b(plan_id, "0" * 64, minutes(30), by="manager:test")
    assert err.value.detail["current_hash"] == digest
    outcome = p.approve_plan_b(plan_id, digest, minutes(30), by="manager:test")
    assert outcome["option"] == "lead"
    assert school.execute("SELECT status FROM shift WHERE id = %s", (HERO,)).fetchone() == ("scheduled",)
    assert school.execute("SELECT status FROM cover_case WHERE id = %s", (case,)).fetchone() == ("plan_b",)


def test_merge_within_the_norm_tells_the_families(school):
    # make the merge legal: older children (norm 1:8) and 4 + 4 of them
    school.execute("UPDATE lesson_group SET age_min = 7, age_max = 9, size = 4 WHERE id IN ('G001', 'G002')")
    p = pipeline(school)
    cases, _ = voice_callout(p)
    case = cases[HERO]
    decline_all(school, p, case)
    plan_id, snapshot, digest = school.execute(
        "SELECT id, snapshot, snapshot_hash FROM plan_b WHERE case_id = %s AND status = 'proposed'", (case,)
    ).fetchone()
    merge = snapshot["options"][0]
    assert (merge["kind"], merge["into_shift_id"], merge["participants"], merge["capacity"]) == ("merge", NEXT_DOOR, 8, 8)
    outcome = p.approve_plan_b(plan_id, digest, minutes(30), by="manager:test")
    assert outcome == {"plan_id": plan_id, "option": "merge", "families": 8}
    told = school.execute(
        "SELECT DISTINCT m.group_id FROM message m WHERE m.case_id = %s AND m.audience = 'family'", (case,)
    ).fetchall()
    assert {g for (g,) in told} == {"G001", "G002"}
    assert [g for (g,) in school.execute("SELECT group_id FROM shift_group WHERE shift_id = %s ORDER BY 1", (NEXT_DOOR,))] == ["G001", "G002"]


def test_a_changed_situation_refuses_the_shown_plan(school):
    p = pipeline(school)
    cases, _ = voice_callout(p)
    case = cases[HERO]
    decline_all(school, p, case)
    plan_id, digest = school.execute("SELECT id, snapshot_hash FROM plan_b WHERE case_id = %s AND status = 'proposed'", (case,)).fetchone()
    p.closure("V1", "V1-TEACH", "equipment", NOW.replace(hour=6, minute=0), NOW.replace(hour=8, minute=0),
              now=minutes(20), created_by="manager:test")  # the group next door is gone: the facts changed
    with pytest.raises(Conflict, match="plan changed") as err:
        p.approve_plan_b(plan_id, digest, minutes(21), by="manager:test")
    new_id = int(err.value.detail["plan_id"].removeprefix("P-"))
    assert new_id != plan_id and err.value.detail["current_hash"] != digest
    assert school.execute("SELECT status FROM plan_b WHERE id = %s", (plan_id,)).fetchone() == ("superseded",)
    with pytest.raises(Conflict, match="plan changed"):  # the old plan stays refused, pointing to the current one
        p.approve_plan_b(plan_id, digest, minutes(22), by="manager:test")
    assert p.approve_plan_b(new_id, err.value.detail["current_hash"], minutes(22), by="manager:test")["option"] == "lead"


# scenario 3: closure -> lessons cancelled, families told in their languages, credits
def test_closure_cancels_lessons_and_gives_credits(school):
    p = pipeline(school)
    start, end = NOW.replace(hour=6, minute=0), NOW.replace(hour=8, minute=0)
    result = p.closure("V1", "V1-TEACH", "equipment", start, end, now=minutes(10), created_by="manager:test")
    assert result["cancelled_shifts"] == [NEXT_DOOR, "L-1109-G003"]
    assert result["remedy"] == "credit" and result["families"] == 6 + 3
    assert set(result["languages"]) == {"es", "en"}
    remedies = school.execute("SELECT group_id, kind, families FROM remedy WHERE closure_id = %s ORDER BY 1", (result["closure_id"],)).fetchall()
    assert remedies == [("G001", "credit", 6), ("G003", "credit", 3)]
    texts = school.execute("SELECT language, body FROM message WHERE closure_id = %s ORDER BY id", (result["closure_id"],)).fetchall()
    assert all("{group}" not in body and "{time}" not in body for _, body in texts)
    assert any("falla del equipo" in body for lang, body in texts if lang == "es")
    with pytest.raises(ValueError):
        p.closure("V1", None, "aliens", start, end, now=minutes(10), created_by="manager:test")


# autonomy: "propose" leaves a card, "off" does nothing, "auto" acts
def test_assign_cover_propose_waits_for_a_person(school):
    p = pipeline(school, assign_cover="propose")
    cases, _ = voice_callout(p)
    case = cases[HERO]
    offer_id, staff_id = pending(school, case)
    p.respond(offer_id, True, minutes(2))
    approval_id, status = school.execute("SELECT id, status FROM approval WHERE case_id = %s", (case,)).fetchone()
    assert status == "pending"
    assert school.execute("SELECT status FROM shift WHERE id = %s", (HERO,)).fetchone() == ("open",)
    p.decide_approval(approval_id, True, minutes(4), by="manager:test")
    assert school.execute("SELECT staff_id, status FROM shift WHERE id = %s", (HERO,)).fetchone() == (staff_id, "scheduled")


def test_assign_cover_off_sends_no_offers(school):
    p = pipeline(school, assign_cover="off")
    cases, _ = voice_callout(p)
    assert school.execute("SELECT count(*) FROM offer").fetchone() == (0,)
    assert "offers_off" in events(school, cases[HERO])


def test_plan_b_on_auto_applies_without_a_person(school):
    p = pipeline(school, plan_b="auto")
    cases, _ = voice_callout(p)
    decline_all(school, p, cases[HERO])
    assert school.execute("SELECT status, decided_by FROM plan_b WHERE case_id = %s", (cases[HERO],)).fetchone() == ("approved", "agent")


def test_overtime_is_only_proposed_with_its_price_and_then_booked(school):
    p = pipeline(school)
    cases, _ = voice_callout(p)
    case = cases[HERO]
    decline_all(school, p, case)
    plan_id, snapshot, digest = school.execute(
        "SELECT id, snapshot, snapshot_hash FROM plan_b WHERE case_id = %s AND status = 'proposed'", (case,)
    ).fetchone()
    index, option = next((i, o) for i, o in enumerate(snapshot["options"]) if o["kind"] == "overtime")
    assert (option["staff_id"], option["mode"], option["hours_over"], option["cost_usd"]) == ("S11", "propose", 1.5, 51.0)
    p.approve_plan_b(plan_id, digest, minutes(40), by="manager:test", option=index)
    offer_id, staff_id = pending(school, case)
    assert staff_id == "S11"
    state = p.respond(offer_id, True, minutes(41))
    assert state["status"] == "accepted" and state["case_status"] == "covered"


# the person is checked again at the moment of "yes"; a late answer is refused
def test_yes_rechecks_the_rules_and_moves_on(school):
    p = pipeline(school)
    cases, _ = voice_callout(p)
    case = cases[HERO]
    offer_id, staff_id = pending(school, case)
    school.execute(
        "INSERT INTO shift (id, venue_id, role_id, starts_at, ends_at, staff_id)"
        " SELECT 'X-CLASH', 'V2', role_id, starts_at, ends_at, %s FROM shift WHERE id = %s",
        (staff_id, HERO),
    )
    state = p.respond(offer_id, True, minutes(2))
    assert state["status"] == "withdrawn" and "already booked" in state["reason"]
    assert "accept_rejected" in events(school, case)
    nxt, nxt_staff = pending(school, case)
    assert nxt is not None and nxt_staff != staff_id


def test_a_late_answer_is_refused_and_the_queue_moves_on(school):
    p = pipeline(school)
    cases, _ = voice_callout(p)
    case = cases[HERO]
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
