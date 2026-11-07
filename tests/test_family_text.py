"""The message to a private lesson's family: the facts say what to tell them, and a model's text goes out only if it
says that (who can't come, who teaches instead or the new day, the time, a yes/no question about it, no machine date).
Otherwise the template goes out and the case file keeps the rejected draft.

The live model is a fake transport here: nothing leaves the machine, no model is called.
"""

import json
from datetime import date, timedelta

import pytest

from coachcover import messages as M
from coachcover import seed
from coachcover.demo import CALLOUT_DELAY, VOICES
from coachcover.models import TEXT_MODEL, DbLedger, ModelClient
from coachcover.pipeline import Pipeline

MONDAY, TUESDAY, WEDNESDAY = date(2026, 11, 9), date(2026, 11, 10), date(2026, 11, 11)
PRIVATE = seed.HERO_PRIVATE
# what the live model wrote on the stand (2026-11-07), word for word
VAGUE_COVER = "Hi! Just a warm check-in about Lily with Chloe and Marina on 2026-11-09 at 17:00. Can you make it?"
VAGUE_MOVE = "Hi! Lily has her lesson with Marina on Wed, Nov 11 at 17:00. Would you like to reschedule?"
GOOD_COVER = "Hi! Marina can't make it today, so Chloe can teach Lily's private lesson at 17:00 instead. Is that OK? Reply YES or NO."
GOOD_MOVE = "No problem, Lily stays with Marina. Her lesson moves to Wed, Nov 11 at 17:00 with Marina. Does that work? Reply YES or NO."


def cover(lang="en"):
    return M.family_facts("cover", language=lang, child="Lily", coach="Chloe", absent="Marina", day=MONDAY, today=MONDAY,
                          time="17:00", venue="Riverside Aquatic Center")


def move(lang="en"):
    return M.family_facts("reschedule", language=lang, child="Lily", coach="Marina", day=WEDNESDAY, today=MONDAY,
                          time="17:00", venue="Riverside Aquatic Center")


# --- the facts and the check (pure) ---------------------------------------------------------------------------------


def test_days_are_written_for_people_in_the_familys_language():
    assert [M.day_label(d, MONDAY, "en") for d in (MONDAY, TUESDAY, WEDNESDAY)] == ["today", "tomorrow", "Wed, Nov 11"]
    assert [M.day_label(d, MONDAY, "es") for d in (MONDAY, TUESDAY, WEDNESDAY)] == ["hoy", "mañana", "mié 11 nov"]
    assert [M.day_label(d, MONDAY, "ru") for d in (MONDAY, TUESDAY, WEDNESDAY)] == ["сегодня", "завтра", "ср, 11 ноя"]
    for facts in (cover(), move(), cover("ru"), move("es")):
        assert not M.ISO_DATE.search(json.dumps(facts, ensure_ascii=False))  # no machine date reaches the model
    assert cover()["say"] == ("Marina can't teach Lily's private lesson today at 17:00. Chloe can teach it instead, same "
                              "time and same place. Ask the parent if that is OK.")
    assert move()["say"] == ("Lily keeps their own coach, Marina. The lesson moves to Wed, Nov 11 at 17:00 with Marina. "
                             "Ask the parent if that works.")


@pytest.mark.parametrize("lang", ["en", "es", "ru"])
def test_the_templates_pass_the_check_in_every_language(lang):
    c, m = cover(lang), move(lang)
    assert M.family_text_problems("cover", c, M.CLIENT_COVER[lang].format(**c), MONDAY) == []
    assert M.family_text_problems("reschedule", m, M.CLIENT_RESCHEDULE[lang].format(**m), WEDNESDAY) == []


def test_the_check_refuses_what_the_live_model_wrote_and_passes_a_clear_text():
    vague = M.family_text_problems("cover", cover(), VAGUE_COVER, MONDAY)
    assert vague == ["a machine date (YYYY-MM-DD)", "does not say that another coach teaches instead",
                     "the question does not ask about this change: 'can you make it?'"]
    assert M.family_text_problems("reschedule", move(), VAGUE_MOVE, WEDNESDAY) == [
        "does not say that the lesson moves", "the question does not ask about this change: 'would you like to reschedule?'"]
    assert M.family_text_problems("cover", cover(), GOOD_COVER, MONDAY) == []
    assert M.family_text_problems("reschedule", move(), GOOD_MOVE, WEDNESDAY) == []
    # each part on its own: a name, the time, the new day, the question at the end
    assert M.family_text_problems("cover", cover(), GOOD_COVER.replace("Marina", "your coach"), MONDAY) == ["does not name Marina"]
    assert M.family_text_problems("cover", cover(), GOOD_COVER.replace(" at 17:00", ""), MONDAY) == ["no time 17:00"]
    assert M.family_text_problems("cover", cover(), GOOD_COVER.replace("17:00", "5 PM"), MONDAY) == []  # 12-hour is fine
    assert M.family_text_problems("reschedule", move(), GOOD_MOVE.replace("Wed, Nov 11", "Wednesday"), WEDNESDAY) == [
        "no new day Wed, Nov 11"]
    assert M.family_text_problems("cover", cover(), "Marina can't come today; Chloe teaches Lily at 17:00 instead.",
                                  MONDAY) == ["does not end with a yes/no question"]
    assert M.family_text_problems("cover", cover(), GOOD_COVER.replace("today", "on 2026-11-09"), MONDAY) == [
        "a machine date (YYYY-MM-DD)"]


# --- the pipeline with a live model (fake transport) ----------------------------------------------------------------


@pytest.fixture
def asked(new_database):
    """Marina's call-out on the replay; then a live model (fake transport) writes the question to Lily's family when
    Chloe says yes. Returns (conn, pipeline, case, set the model's next text)."""
    conn = new_database()
    seed.load_school(conn)
    replay = Pipeline(conn, ModelClient(DbLedger(conn), mode="replay"), seed.POLICY_ID)
    voice = VOICES["preschool"]
    replay.callout("S05", now=seed.NOW + CALLOUT_DELAY["preschool"], audio=voice.read_bytes(), filename=voice.name)
    case = conn.execute("SELECT id FROM cover_case WHERE shift_id = %s", (PRIVATE,)).fetchone()[0]
    texts, sent = [], []

    def transport(path, request):
        body = request["json"]
        sent.append(body)
        assert path == "/chat/completions" and body["response_format"]["json_schema"]["name"] == "family_question"
        return {"model": TEXT_MODEL, "usage": {"prompt_tokens": 250, "completion_tokens": 50},
                "choices": [{"message": {"content": json.dumps({"body": texts.pop(0)})}}]}, 900

    live = Pipeline(conn, ModelClient(DbLedger(conn), mode="live", api_key="test-only", transport=transport), seed.POLICY_ID)
    return conn, live, case, texts, sent


def chloe_yes(conn, p, case, at):
    offer = conn.execute("SELECT id FROM offer WHERE case_id = %s AND staff_id = 'S18'", (case,)).fetchone()[0]
    p.respond(offer, True, at)


def last_question(conn, case):
    body = conn.execute("SELECT body FROM message WHERE case_id = %s AND audience = 'family' ORDER BY id DESC LIMIT 1",
                        (case,)).fetchone()[0]
    event = conn.execute("SELECT data FROM case_event WHERE case_id = %s AND kind = 'family_asked' ORDER BY seq DESC"
                         " LIMIT 1", (case,)).fetchone()[0]
    return body, event


@pytest.mark.db
def test_a_vague_live_text_is_not_sent_the_template_is(asked):
    conn, p, case, texts, sent = asked
    texts.append(VAGUE_COVER)
    chloe_yes(conn, p, case, seed.NOW + timedelta(minutes=2))
    body, event = last_question(conn, case)
    assert body == ("CoachCover: Marina can't make it today, so Chloe would teach Lily's private lesson at 17:00 instead. "
                    "Same time, same place. Is that OK? Reply YES or NO.")
    assert event["text_source"] == "template" and event["model_text"] == VAGUE_COVER
    assert "a machine date (YYYY-MM-DD)" in event["model_text_rejected"]
    facts = json.loads(sent[0]["messages"][1]["content"])  # what the model was told: the change, in plain words
    assert (facts["day"], facts["absent"], facts["coach"]) == ("today", "Marina", "Chloe") and "instead" in facts["say"]
    assert not M.ISO_DATE.search(sent[0]["messages"][1]["content"])
    call = conn.execute("SELECT purpose, source, ok FROM model_call ORDER BY id DESC LIMIT 1").fetchone()
    assert call == ("family_question", "live", True)  # the call itself worked; its text did not pass


@pytest.mark.db
def test_a_clear_live_text_is_sent_as_written(asked):
    conn, p, case, texts, _ = asked
    texts.append(GOOD_COVER)
    chloe_yes(conn, p, case, seed.NOW + timedelta(minutes=2))
    body, event = last_question(conn, case)
    assert body == GOOD_COVER and event["text_source"] == "model" and "model_text_rejected" not in event


@pytest.mark.db
def test_a_machine_date_in_an_otherwise_clear_text_sends_the_template(asked):
    conn, p, case, texts, _ = asked
    texts.append(GOOD_COVER.replace("today", "on 2026-11-09"))
    chloe_yes(conn, p, case, seed.NOW + timedelta(minutes=2))
    body, event = last_question(conn, case)
    assert body.startswith("CoachCover: Marina can't make it today") and event["model_text_rejected"] == [
        "a machine date (YYYY-MM-DD)"]


@pytest.mark.db
def test_would_you_like_to_reschedule_is_not_asked_the_new_time_is(asked):
    conn, p, case, texts, _ = asked
    texts.extend([GOOD_COVER, VAGUE_MOVE])
    chloe_yes(conn, p, case, seed.NOW + timedelta(minutes=2))
    ask = conn.execute("SELECT id FROM client_ask WHERE case_id = %s", (case,)).fetchone()[0]
    p.family_respond(ask, False, seed.NOW + timedelta(minutes=4))
    body, event = last_question(conn, case)
    assert event["kind"] == "reschedule" and event["text_source"] == "template"
    assert body == ("CoachCover: no problem, Lily stays with Marina. The lesson moves to Wed, Nov 11 at 17:00 with Marina. "
                    "Does that work? Reply YES or NO.")
    assert "the question does not ask about this change: 'would you like to reschedule?'" in event["model_text_rejected"]
