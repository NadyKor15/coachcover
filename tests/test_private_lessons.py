"""Private lessons (one child, one coach): another coach only after the family says yes.

Marina's voice note at 08:40:10 opens her 09:30 group lesson and, because she is out for the whole day, her 17:00
private lesson with Lily (L-1109-P001). The first coach to say yes is held (no other offer for that time) and the
family is asked; their "yes" books the coach after the rules run again, their "no" lets the coach go and offers
Marina's nearest free time at the same hour (Wednesday 17:00: Tuesday 17:00 is her lesson with Vera), else a credit
of one lesson at the private price. Silence: the coach is let go and the manager gets plan B (move or credit).
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from coachcover import seed
from coachcover.demo import CALLOUT_DELAY, VOICES, run_scenario
from coachcover.models import DbLedger, ModelClient
from coachcover.money import money_params, uncovered_cost
from coachcover.pipeline import Conflict, Pipeline
from coachcover.presets import default_policy
from coachcover.repo import load_shift
from coachcover.screens import today

pytestmark = pytest.mark.db

NY = ZoneInfo("America/New_York")
HERO, PRIVATE = seed.HERO_SHIFT, seed.HERO_PRIVATE
WAVE = ["S18", "S15", "S25"]  # Chloe 5.75 h, Diego 5.75 h, Sofia 6 h: cleared for Level 2 at 17:00, fewest hours


def at(hhmm: str, day: int = 9) -> datetime:
    h, m, *s = (int(x) for x in hhmm.split(":"))
    return datetime(2026, 11, day, h, m, s[0] if s else 0, tzinfo=NY)


@pytest.fixture
def morning(new_database):
    """The preschool story at 08:40:10: Marina's voice note opens both of her Monday lessons."""
    conn = new_database()
    seed.load_school(conn)
    p = Pipeline(conn, ModelClient(DbLedger(conn), mode="replay"), seed.POLICY_ID)
    voice = VOICES["preschool"]
    result = p.callout("S05", now=seed.NOW + CALLOUT_DELAY["preschool"], audio=voice.read_bytes(), filename=voice.name)
    cases = {c["shift_id"]: c["case_id"] for c in result["cases"]}
    return conn, p, cases


def offers(conn, case):
    return {sid: oid for oid, sid in conn.execute("SELECT id, staff_id FROM offer WHERE case_id = %s", (case,))}


def statuses(conn, case):
    return conn.execute("SELECT staff_id, status FROM offer WHERE case_id = %s ORDER BY queue_pos", (case,)).fetchall()


def kinds(conn, case):
    return [k for (k,) in conn.execute("SELECT kind FROM case_event WHERE case_id = %s ORDER BY seq", (case,))]


def asks(conn, case):
    return conn.execute("SELECT id, kind, staff_id, status, proposed_start, answer_by FROM client_ask"
                        " WHERE case_id = %s ORDER BY id", (case,)).fetchall()


def family_texts(conn, case):
    return [b for (b,) in conn.execute("SELECT body FROM message WHERE case_id = %s AND audience = 'family' ORDER BY id",
                                       (case,))]


def chloe_says_yes(conn, p, cases, when="08:42"):
    case = cases[PRIVATE]
    p.respond(offers(conn, case)["S18"], True, at(when))  # the top of the wave: nobody higher can still answer
    return case


# --- the data -------------------------------------------------------------------------------------------------------


def test_the_private_price_is_the_founders_and_the_morning_card_is_unchanged():
    preset = default_policy("swim_school").preset
    price = preset.money["private_lesson_price"]
    # the screen's source line, like the group price's "from the founder's coaching experience"
    assert price["value"] == seed.PRIVATE_LESSON_USD == 50 and price["source"] == "from the founder's coaching practice"
    assert preset.money["lesson_price"]["value"] == 30.0  # the group lesson stays $30 a child
    assert (preset.client_answer_minutes, preset.client_answer_before_start_minutes) == (30, 60)


def test_every_coach_with_a_free_week_has_one_or_two_private_lessons_and_the_deck_story_has_none():
    school = seed.build_school()
    private = [g for g in school.groups if g.get("kind") == "private"]
    per_coach = {}
    for g in private:
        per_coach[g["instructor_id"]] = per_coach.get(g["instructor_id"], 0) + 1
        assert g["size"] == 1 and g["minutes"] == seed.PRIVATE_MINUTES and g["name"].startswith("Private · ")
    assert per_coach["S05"] == 2 and all(1 <= n <= 2 for n in per_coach.values())
    lily = next(g for g in private if g["participant"] == "Lily")
    assert lily["name"] == "Private · Lily, age 6, Level 2" and lily["family_languages"] == ["en"]
    staff_first = {name.split()[0] for _, name, _, _ in school.staff}
    assert not {g["participant"] for g in private} & staff_first  # no child shares a coach's name
    deck = seed.build_school("deck")
    assert not [g for g in deck.groups if g.get("kind") == "private"] and len(deck.groups) == 120
    # nobody's week goes over the greedy cap because of a private lesson; the two near the limit are the story's
    assert sorted(s for s, h in school.demo["hours"].items() if h > seed.GREEDY_HOURS_CAP) == ["S11", "S29"]


def test_a_private_lesson_is_never_merged_or_counted_in_the_morning_card(morning):
    conn, p, cases = morning
    shift = load_shift(conn, PRIVATE)
    assert [g.kind for g in shift.groups] == ["private"]
    params = money_params(default_policy("swim_school").preset.money, 34.0)
    cost = uncovered_cost([shift], params)
    assert cost["total_usd"] == 50.0 and cost["lines"][0]["label"] == "Private lesson credit: 1 × $50"
    screen = today(conn, seed.POLICY_ID, at("08:41"), None, live=False)
    assert screen["money"]["uncovered_morning_usd"] == 240.0  # 8 children x $30: the morning, not Lily's lesson


# --- the call-out ---------------------------------------------------------------------------------------------------


def test_the_call_out_opens_the_rest_of_the_day_and_the_morning_is_unchanged(morning):
    conn, p, cases = morning
    assert list(cases) == [HERO, PRIVATE]  # the lesson the voice note names first, then the rest of her day
    assert conn.execute("SELECT code FROM cover_case WHERE id = %s", (cases[HERO],)).fetchone()[0] == "C-1109-01"
    assert [s for s, _ in statuses(conn, cases[HERO])] == ["S25", "S26", "S17"]  # Sofia, Leo, Zoe: O-1..O-3 as before
    assert sorted(offers(conn, cases[HERO]).values()) == [1, 2, 3]
    assert [s for s, _ in statuses(conn, cases[PRIVATE])] == WAVE
    opened = conn.execute("SELECT data FROM case_event WHERE case_id = %s AND kind = 'shift_opened'",
                          (cases[PRIVATE],)).fetchone()[0]
    assert opened["why"] == "out for the whole day" and opened["private"] is True
    offer = conn.execute("SELECT body FROM message WHERE offer_id = %s", (offers(conn, cases[PRIVATE])["S18"],)).fetchone()[0]
    assert "private lesson" in offer


def test_the_founders_morning_still_ends_with_zoe_at_0845(new_database):
    conn = new_database()
    result = run_scenario(conn, ModelClient(DbLedger(conn), mode="replay"), "dangerous_morning")
    assert result["case_id"] == "C-1109-01" and result["ended_at"] == at("08:45:00")
    ranked = conn.execute("SELECT c.data FROM case_event c JOIN cover_case k ON k.id = c.case_id"
                          " WHERE k.code = 'C-1109-01' AND c.kind = 'candidates_ranked'").fetchone()[0]
    assert (ranked["total"], ranked["eligible"]) == (40, 5)
    assert conn.execute("SELECT staff_id FROM shift WHERE id = %s", (HERO,)).fetchone() == ("S17",)
    assert conn.execute("SELECT status FROM cover_case WHERE shift_id = %s", (PRIVATE,)).fetchone() == ("open",)


# --- yes ------------------------------------------------------------------------------------------------------------


def test_a_coachs_yes_waits_for_the_familys_yes_then_books(morning):
    conn, p, cases = morning
    case = chloe_says_yes(conn, p, cases)
    assert statuses(conn, case) == [("S18", "held"), ("S15", "withdrawn"), ("S25", "withdrawn")]
    assert conn.execute("SELECT staff_id, status FROM shift WHERE id = %s", (PRIVATE,)).fetchone() == (None, "open")
    [(ask, kind, staff, status, _, answer_by)] = asks(conn, case)
    assert (kind, staff, status, answer_by) == ("cover", "S18", "asked", at("09:12"))  # 30 min to answer
    [question] = family_texts(conn, case)
    assert "Chloe" in question and "Lily" in question and "Marina" in question and "17:00" in question
    p.family_respond(ask, True, at("08:46"))
    assert conn.execute("SELECT staff_id, status FROM shift WHERE id = %s", (PRIVATE,)).fetchone() == ("S18", "scheduled")
    assert conn.execute("SELECT status FROM cover_case WHERE id = %s", (case,)).fetchone() == ("covered",)
    assert statuses(conn, case)[0] == ("S18", "accepted")
    seq = kinds(conn, case)
    assert seq.index("family_asked") < seq.index("family_yes") < seq.index("shift_covered")
    coach = [b for (b,) in conn.execute("SELECT body FROM message WHERE case_id = %s AND staff_id = 'S18' ORDER BY id",
                                        (case,))]
    assert "checking with the family" in coach[-2] and coach[-1].startswith("CoachCover: confirmed")
    assert family_texts(conn, case)[-1] == "CoachCover: thank you! Chloe will teach Lily today at 17:00."


def test_the_held_coach_gets_no_other_offer_for_that_time(morning):
    conn, p, cases = morning
    case = chloe_says_yes(conn, p, cases)
    # another lesson at the same hour, that Chloe could otherwise teach
    other = conn.execute(
        "INSERT INTO shift (id, venue_id, zone_id, role_id, starts_at, ends_at, staff_id, instructors, extra_certs)"
        " SELECT 'L-TEST-1700', venue_id, zone_id, role_id, starts_at, ends_at, 'S02', 1, '{}' FROM shift WHERE id = %s"
        " RETURNING id", (PRIVATE,)).fetchone()[0]
    conn.execute("INSERT INTO shift_group (shift_id, group_id) VALUES (%s, 'P004')", (other,))
    grid = {g["staff_id"]: g for g in p.candidates(other, at("08:43"))["grid"]}
    assert "OVERLAPS_COMMITMENT" in grid["S18"]["blocking_codes"]  # held for Lily's family
    [(ask, *_)] = asks(conn, case)
    p.family_respond(ask, False, at("08:44"))  # the family says no: Chloe is free again
    grid = {g["staff_id"]: g for g in p.candidates(other, at("08:45"))["grid"]}
    assert "OVERLAPS_COMMITMENT" not in grid["S18"]["blocking_codes"]


def test_if_the_coach_no_longer_passes_at_the_familys_yes_the_next_coach_is_asked(morning):
    conn, p, cases = morning
    case = chloe_says_yes(conn, p, cases)
    [(ask, *_)] = asks(conn, case)
    # Chloe's instructor card turns out to be revoked before the family answers (the issuer's registry)
    conn.execute("UPDATE issuer_registry SET revoked = true, revoked_on = '2026-11-09' WHERE card_number ="
                 " (SELECT sc.card_number FROM staff_cert sc JOIN cert_type c ON c.id = sc.cert_type_id"
                 "  WHERE sc.staff_id = 'S18' AND c.code = 'wsi')")
    p.family_respond(ask, True, at("08:46"))
    assert conn.execute("SELECT staff_id, status FROM shift WHERE id = %s", (PRIVATE,)).fetchone() == (None, "open")
    assert "family_yes_recheck_failed" in kinds(conn, case)
    assert family_texts(conn, case)[-1] == "CoachCover: sorry, Chloe can't take the lesson after all. We'll suggest another coach shortly."
    second = [s for s, st in statuses(conn, case) if st == "pending"]
    assert second and not set(second) & set(WAVE)  # the next wave: people not asked yet
    p.respond(offers(conn, case)[second[0]], True, at("08:47"))
    rows = asks(conn, case)
    assert [(k, s, st) for _, k, s, st, _, _ in rows] == [("cover", "S18", "yes"), ("cover", second[0], "asked")]


# --- no -------------------------------------------------------------------------------------------------------------


def test_no_moves_the_lesson_to_the_familys_own_coach(morning):
    conn, p, cases = morning
    case = chloe_says_yes(conn, p, cases)
    [(ask, *_)] = asks(conn, case)
    p.family_respond(ask, False, at("08:44"))
    assert statuses(conn, case)[0] == ("S18", "withdrawn")
    released = conn.execute("SELECT body FROM message WHERE case_id = %s AND staff_id = 'S18' ORDER BY id DESC LIMIT 1",
                            (case,)).fetchone()[0]
    assert released.startswith("CoachCover: thank you! The family would rather wait for their own coach")
    checked = conn.execute("SELECT data FROM case_event WHERE case_id = %s AND kind = 'own_coach_checked'",
                           (case,)).fetchone()[0]
    assert checked["tried"][0]["codes"] == ["OVERLAPS_COMMITMENT"]  # Tuesday 17:00 is her lesson with Vera
    rows = asks(conn, case)
    assert [(k, s, st) for _, k, s, st, _, _ in rows] == [("cover", "S18", "no"), ("reschedule", "S05", "asked")]
    assert rows[1][4] == at("17:00", day=11)  # Wednesday 17:00
    assert "Wed, Nov 11 at 17:00" in family_texts(conn, case)[-1]
    p.family_respond(rows[1][0], True, at("08:46"))
    assert conn.execute("SELECT status FROM shift WHERE id = %s", (PRIVATE,)).fetchone() == ("cancelled",)
    moved = conn.execute("SELECT staff_id, status, starts_at FROM shift WHERE id = 'L-1111-P001'").fetchone()
    assert moved == ("S05", "scheduled", at("17:00", day=11))
    assert conn.execute("SELECT status FROM cover_case WHERE id = %s", (case,)).fetchone() == ("plan_b",)
    line = conn.execute("SELECT data->>'line' FROM case_event WHERE case_id = %s AND kind = 'private_moved'",
                        (case,)).fetchone()[0]
    assert line == "Moved to Marina, Wed 17:00"
    assert not conn.execute("SELECT 1 FROM remedy WHERE case_id = %s", (case,)).fetchone()  # nothing is credited


def test_no_to_the_new_time_gives_a_credit_for_one_lesson(morning):
    conn, p, cases = morning
    case = chloe_says_yes(conn, p, cases)
    p.family_respond(asks(conn, case)[0][0], False, at("08:44"))
    p.family_respond(asks(conn, case)[1][0], False, at("08:46"))
    assert conn.execute("SELECT kind, families, amount_usd FROM remedy WHERE case_id = %s", (case,)).fetchall() == [
        ("credit", 1, 50)]
    assert conn.execute("SELECT status FROM shift WHERE id = %s", (PRIVATE,)).fetchone() == ("cancelled",)
    assert family_texts(conn, case)[-1] == ("CoachCover: sorry, Lily's private lesson on Mon 17:00 is cancelled."
                                            " You have a credit for one lesson ($50).")


def test_no_and_no_free_time_for_the_own_coach_gives_a_credit(morning):
    conn, p, cases = morning
    case = chloe_says_yes(conn, p, cases)
    conn.execute("UPDATE staff_cert SET expires_on = '2026-11-09' WHERE staff_id = 'S05'")  # Marina's cards end today
    p.family_respond(asks(conn, case)[0][0], False, at("08:44"))
    assert [k for _, k, *_ in asks(conn, case)] == ["cover"]  # nothing to propose: no second question
    assert conn.execute("SELECT data->>'why' FROM case_event WHERE case_id = %s AND kind = 'private_credit'",
                        (case,)).fetchone()[0] == "no_time"
    assert conn.execute("SELECT amount_usd FROM remedy WHERE case_id = %s", (case,)).fetchone() == (50,)


def test_an_answered_question_cannot_be_answered_again(morning):
    conn, p, cases = morning
    case = chloe_says_yes(conn, p, cases)
    ask = asks(conn, case)[0][0]
    p.family_respond(ask, True, at("08:46"))
    with pytest.raises(Conflict, match="already closed"):
        p.family_respond(ask, False, at("08:47"))


# --- silence --------------------------------------------------------------------------------------------------------


def test_silence_lets_the_coach_go_and_the_manager_decides(morning):
    conn, p, cases = morning
    case = chloe_says_yes(conn, p, cases)
    p.expire_due(at("09:11"))
    assert asks(conn, case)[0][3] == "asked"
    p.expire_due(at("09:12"))  # 30 min without an answer
    assert asks(conn, case)[0][3] == "silent" and statuses(conn, case)[0] == ("S18", "withdrawn")
    plan = conn.execute("SELECT id, snapshot, snapshot_hash, status FROM plan_b WHERE case_id = %s", (case,)).fetchone()
    assert plan[3] == "proposed"
    assert [(o["kind"], o["mode"]) for o in plan[1]["options"]] == [("reschedule_own", "propose"), ("credit", "propose")]
    assert plan[1]["options"][0]["when"] == "Wed 17:00" and plan[1]["options"][1]["amount_usd"] == 50.0
    # nothing moves or is credited until the manager approves
    assert conn.execute("SELECT status FROM shift WHERE id = %s", (PRIVATE,)).fetchone() == ("open",)
    assert not conn.execute("SELECT 1 FROM remedy WHERE case_id = %s", (case,)).fetchone()
    p.approve_plan_b(plan[0], plan[2], at("09:20"), by="Natalie Brooks", option=1)
    assert conn.execute("SELECT amount_usd FROM remedy WHERE case_id = %s", (case,)).fetchone() == (50,)


def test_the_family_answers_at_the_latest_an_hour_before_the_lesson(morning):
    conn, p, cases = morning
    case = cases[PRIVATE]
    conn.execute("UPDATE offer SET expires_at = %s WHERE case_id = %s", (at("16:30"), case))  # still open at 15:40
    for sid in ("S15", "S25"):
        p.respond(offers(conn, case)[sid], False, at("15:40"))
    p.respond(offers(conn, case)["S18"], True, at("15:45"))
    assert asks(conn, case)[0][5] == at("16:00")  # 17:00 - 60 min, sooner than 15:45 + 30 min
    with pytest.raises(Conflict):
        p.family_respond(asks(conn, case)[0][0], True, at("16:01"))  # too late: the manager has it
    assert asks(conn, case)[0][3] == "silent"


def test_the_judge_scenarios_show_the_question_to_the_family(new_database):
    conn = new_database()
    result = run_scenario(conn, ModelClient(DbLedger(conn), mode="replay"), "private_lesson")
    assert result["case_id"] == "C-1109-02"
    screen = today(conn, seed.POLICY_ID, result["ended_at"], None, live=False)
    labels = [t.get("label") for t in screen["timeline"]]
    assert "Asked the parent" in labels and "Parent said yes" in labels
    assert conn.execute("SELECT staff_id FROM shift WHERE id = %s", (HERO,)).fetchone() == ("S17",)  # the morning too
    calls = conn.execute("SELECT purpose, ok, coalesce((ref->>'synthetic')::boolean, false) FROM model_call").fetchall()
    assert all(ok and synthetic for _, ok, synthetic in calls)  # every model answer prepared, none live
    conn2 = new_database()
    result = run_scenario(conn2, ModelClient(DbLedger(conn2), mode="replay"), "private_declined")
    screen = today(conn2, seed.POLICY_ID, result["ended_at"], None, live=False)
    labels = [t.get("label") for t in screen["timeline"]]
    assert "Parent said no" in labels and "Moved to Marina, Wed 17:00" in labels
    assert date(2026, 11, 11) == conn2.execute("SELECT (starts_at AT TIME ZONE 'America/New_York')::date FROM shift"
                                               " WHERE id = 'L-1111-P001'").fetchone()[0]
    assert timedelta(0) <= result["ended_at"] - at("08:46") < timedelta(minutes=1)


def test_the_case_page_shows_the_schools_local_time(morning):
    """The case file reads in the venue's time, like the voice note and Today: 08:40:10 ET; UTC stays in machine fields."""
    from coachcover.screens import case_page

    conn, p, cases = morning
    page = case_page(conn, seed.POLICY_ID, cases[HERO], live=False)
    first = page["steps"][0]
    assert first["time_label"] == "08:40:10 ET" and first["at"] == "2026-11-09T13:40:10Z"
    assert all(s["time_label"].endswith(" ET") for s in page["steps"])
    queue = [o["why_first"] for o in page["steps"][3]["offers"]] if page["steps"][3]["kind"] == "offers" else []
    ranked = conn.execute("SELECT data FROM case_event WHERE case_id = %s AND kind = 'candidates_ranked'",
                          (cases[HERO],)).fetchone()[0]
    assert ranked["top"][3:] == ["Mateo fourth: 15 h this week, Preschool, 21.7 km away",
                                 "Ethan fifth: 16 h this week, Preschool, 21.7 km away"]
    assert queue == [] or queue[0] == "Sofia first: 6 h this week, Preschool, same pool"


def test_a_yes_too_close_to_the_start_goes_to_the_manager_not_the_family(morning):
    conn, p, cases = morning
    case = cases[PRIVATE]
    conn.execute("UPDATE offer SET expires_at = %s WHERE case_id = %s", (at("16:30"), case))
    p.respond(offers(conn, case)["S18"], True, at("16:05"))  # under an hour before 17:00: no time to ask
    assert asks(conn, case) == [] and statuses(conn, case)[0] == ("S18", "withdrawn")
    assert "family_too_late" in kinds(conn, case)
    late = conn.execute("SELECT body FROM message WHERE case_id = %s AND staff_id = 'S18' ORDER BY id DESC LIMIT 1",
                        (case,)).fetchone()[0]
    assert "too close to the start to check with the family" in late
    plan = conn.execute("SELECT snapshot FROM plan_b WHERE case_id = %s AND status = 'proposed'", (case,)).fetchone()[0]
    assert [o["kind"] for o in plan["options"]] == ["reschedule_own", "credit"]
    assert conn.execute("SELECT staff_id, status FROM shift WHERE id = %s", (PRIVATE,)).fetchone() == (None, "open")


def test_the_screens_speak_of_a_private_lesson_in_plain_words(morning):
    """What the judge sees while the family is asked, and after they moved the lesson: no group words, no codes."""
    from coachcover.screens import case_page

    conn, p, cases = morning
    case = chloe_says_yes(conn, p, cases)
    conn.execute("INSERT INTO ops.judge_state (id, running, scenario, case_code) VALUES (true, false, 'private_lesson',"
                 " 'C-1109-02') ON CONFLICT (id) DO UPDATE SET case_code = 'C-1109-02'")
    screen = today(conn, seed.POLICY_ID, at("08:43"), None, live=False)
    assert screen["outcome"]["family_wait_until"] == "09:12"
    chloe = next(o for o in screen["offers"] if o["staff"] == "Chloe Hayes")
    assert chloe["result_label"] == "said yes, waiting for the family"
    refusal = screen["eligibility"]["first_refusal"]["label"]
    assert refusal.endswith("· private lesson") and "kids" not in refusal and "ratio" not in refusal
    assert screen["plan_b"]["text"] == "move the lesson to Marina, Wed 17:00 · cancel: a credit for one lesson ($50)"
    ask = asks(conn, case)[0][0]
    p.family_respond(ask, False, at("08:44"))
    p.family_respond(asks(conn, case)[1][0], True, at("08:46"))
    page = case_page(conn, seed.POLICY_ID, case, live=False)
    assert page["status_label"] == "Moved" and page["risk"]["private"] is True and page["risk"]["kids"] is None
    assert page["risk"]["total_usd"] == 50.0
